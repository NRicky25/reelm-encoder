from pathlib import Path

import pytest

from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, parse_probe


def sizes(w, h):
    return [(r.width, r.height) for r in pick_rungs(w, h)]


def labels(w, h):
    return [r.label for r in pick_rungs(w, h)]


def test_full_hd_gets_three_levels():
    assert sizes(1920, 1080) == [(1920, 1080), (1280, 720), (854, 480)]
    assert pick_rungs(1920, 1080)[0].video_kbps == 5000
    assert labels(1920, 1080) == ["1080p", "720p", "480p"]


def test_wide_cinema_keeps_its_shape():
    assert sizes(1920, 816) == [(1920, 816), (1280, 544), (854, 362)]


@pytest.mark.parametrize("w,h", [(1920, 816), (1920, 800), (1998, 1080)])
def test_wide_cinema_is_labelled_like_16_9(w, h):
    # 1920x800 is full-HD width with the black bars left out: it's "1080p", not "800p".
    assert labels(w, h) == ["1080p", "720p", "480p"]


def test_labels_for_other_shapes():
    assert labels(1440, 1080)[0] == "1080p"  # 4:3 goes by its height
    assert labels(640, 480) == ["480p"]
    assert labels(1366, 768)[0] == "768p"


def test_portrait_phone_video():
    assert sizes(1080, 1920) == [(1080, 1920), (720, 1280), (480, 854)]
    assert labels(1080, 1920) == ["1080p", "720p", "480p"]
    full_hd = pick_rungs(1920, 1080)[0].video_kbps
    assert pick_rungs(1080, 1920)[0].video_kbps == full_hd, "same pixels, same bitrate"


def test_never_upscales_and_4k_is_capped():
    assert sizes(1280, 720)[0] == (1280, 720)
    assert sizes(3840, 2160)[0] == (1920, 1080)
    assert sizes(2160, 3840)[0] == (1080, 1920)
    assert sizes(640, 360) == [(640, 360)]


@pytest.mark.parametrize(
    "size", [(1920, 1080), (960, 720), (1440, 1080), (1003, 563), (1080, 1920), (640, 480)]
)
def test_bitrate_always_drops_with_quality(size):
    kbps = [r.video_kbps for r in pick_rungs(*size)]
    assert kbps == sorted(kbps, reverse=True) and len(set(kbps)) == len(kbps), kbps


def test_odd_sizes_become_even():
    for w, h in sizes(1001, 563):
        assert w % 2 == 0 and h % 2 == 0


def test_close_sizes_are_not_duplicated():
    assert [w for w, _ in sizes(1366, 768)] == [1366, 854]  # 1280 too close to 1366


def probe_info(**video):
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "width": 1920, "height": 1080,
             "avg_frame_rate": "24000/1001"} | video,
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
        "format": {"duration": "61.5"},
    }  # fmt: skip


def test_parse_probe():
    s = parse_probe(Path("a.mp4"), probe_info())
    assert (s.width, s.height, s.duration, s.has_audio) == (1920, 1080, 61.5, True)
    assert (s.video_index, s.audio_index, round(s.fps, 3), s.hdr) == (0, 1, 23.976, False)


def test_hdr_is_detected():
    assert parse_probe(Path("a.mkv"), probe_info(color_transfer="smpte2084")).hdr
    assert parse_probe(Path("a.mkv"), probe_info(color_transfer="arib-std-b67")).hdr


def test_rotated_phone_video_is_portrait():
    s = parse_probe(Path("a.mp4"), probe_info(side_data_list=[{"rotation": -90}]))
    assert (s.width, s.height) == (1080, 1920)


def test_cover_art_is_not_the_video():
    info = probe_info()
    info["streams"][0]["index"] = 1
    info["streams"][1]["index"] = 2
    info["streams"].insert(
        0,
        {"index": 0, "codec_type": "video", "width": 600, "height": 600,
         "disposition": {"attached_pic": 1}},
    )  # fmt: skip
    s = parse_probe(Path("a.mp4"), info)
    assert s.width == 1920 and s.video_index == 1, "the real video, not the cover picture"


def test_main_audio_track_is_chosen():
    info = probe_info()
    info["streams"] += [
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6},
        {"index": 3, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "disposition": {"default": 1}},
    ]  # fmt: skip
    assert parse_probe(Path("a.mkv"), info).audio_index == 3, "the default track wins"
    info["streams"][3]["disposition"] = {}
    assert parse_probe(Path("a.mkv"), info).audio_index == 2, "else the most channels"


def test_no_video_or_length_is_an_error():
    with pytest.raises(ProbeError, match="no video"):
        parse_probe(
            Path("a.mp3"), {"streams": [{"codec_type": "audio"}], "format": {"duration": "3"}}
        )
    with pytest.raises(ProbeError, match="how long"):
        parse_probe(Path("a.mp4"), probe_info() | {"format": {}})
    with pytest.raises(ProbeError, match="no video"):
        parse_probe(Path("a.mp4"), {"streams": [{"codec_type": "video"}]})  # no size
