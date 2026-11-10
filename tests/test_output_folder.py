"""Encoding into a folder the user picks (the desktop window's mode)."""

import re
import time

import pytest

from reelm_encoder.config import Settings
from reelm_encoder.jobs import INCOMPLETE, JobQueue, folder_slug, new_output_dir
from tests.test_encode import make_clip, needs_ffmpeg


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, work_dir=tmp_path / "work")


def wait_finished(queue: JobQueue, timeout: float = 60) -> list[dict]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        jobs = queue.snapshot()
        if jobs and all(j["status"] in ("done", "failed", "cancelled") for j in jobs):
            return jobs
        time.sleep(0.05)
    raise AssertionError(f"timed out: {queue.snapshot()}")


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Episode 1", "episode-1"),
        ("Paper Kingdoms S2E3 [1080p].final", "paper-kingdoms-s2e3-1080p-final"),
        ("Café — Déjà Vu!", "cafe-deja-vu"),
        ("   ", "video"),
        ("日本語", "video"),
    ],
)
def test_folder_slug_is_safe_in_a_link(name, slug):
    assert folder_slug(name) == slug


def test_new_output_dir_is_named_after_the_video(tmp_path):
    assert new_output_dir(tmp_path, "My Movie") == tmp_path / "My Movie"
    assert (tmp_path / "My Movie").is_dir()


def test_new_output_dir_never_reuses_an_existing_folder(tmp_path):
    (tmp_path / "Movie").mkdir()
    (tmp_path / "Movie" / "keep.txt").write_text("mine")
    assert new_output_dir(tmp_path, "Movie") == tmp_path / "Movie (2)"
    assert new_output_dir(tmp_path, "Movie") == tmp_path / "Movie (3)"
    assert (tmp_path / "Movie" / "keep.txt").read_text() == "mine"


def test_new_output_dir_creates_the_chosen_folder(tmp_path):
    out = new_output_dir(tmp_path / "not" / "there", "clip")
    assert out == tmp_path / "not" / "there" / "clip" and out.is_dir()


@needs_ffmpeg
def test_encode_into_chosen_folder(settings, tmp_path):
    clip = tmp_path / "Episode 1.mp4"
    make_clip(clip, seconds=2, size="640x360")
    out_root = tmp_path / "Encoded"
    queue = JobQueue(settings)
    try:
        queue.add([str(clip)], use_gpu=False, out_root=out_root)
        [job] = wait_finished(queue)
    finally:
        queue.stop()
    assert job["status"] == "done", job
    [folder] = list(out_root.iterdir())
    # link-safe name + a short unique code, so a re-upload never reuses a cached link
    assert re.fullmatch(r"episode-1-[0-9a-f]{6}", folder.name), folder.name
    assert job["folder"] == str(folder)
    assert (folder / "master.m3u8").is_file()
    assert not (folder / INCOMPLETE).exists()
    assert not settings.work_dir.exists() or not any(settings.work_dir.iterdir())


@needs_ffmpeg
def test_cancel_removes_only_the_new_folder(settings, tmp_path):
    clip = tmp_path / "long.mp4"
    make_clip(clip, seconds=60, size="1920x1080")
    out_root = tmp_path / "Encoded"
    (out_root / "long").mkdir(parents=True)
    (out_root / "long" / "earlier.txt").write_text("an earlier encode")

    queue = JobQueue(settings)
    try:
        [job] = queue.add([str(clip)], use_gpu=False, out_root=out_root)
        end = time.monotonic() + 30
        while not (job.status == "encoding" and job.progress > 0) and time.monotonic() < end:
            time.sleep(0.05)
        queue.cancel(job.id)
        [snap] = wait_finished(queue, timeout=15)
    finally:
        queue.stop()
    assert snap["status"] == "cancelled"
    assert [p.name for p in out_root.iterdir()] == ["long"], "the partial encode is removed"
    assert (out_root / "long" / "earlier.txt").read_text() == "an earlier encode"
