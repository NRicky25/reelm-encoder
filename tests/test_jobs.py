"""The encode queue behind the desktop window, and the `encode` command."""

import time

import pytest

from reelm_encoder import cli
from reelm_encoder.config import Settings
from reelm_encoder.jobs import INCOMPLETE, JobQueue, remove_unfinished
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


def test_queued_job_can_be_cancelled(settings, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    queue = JobQueue(settings, start=False)  # no worker: the job stays queued
    [job] = queue.add([str(clip)], use_gpu=False)
    assert not queue.remove(job.id), "can't remove an unfinished job"
    assert queue.cancel(job.id) and job.status == "cancelled"
    assert not queue.cancel(job.id), "already finished"
    assert queue.remove(job.id) and queue.jobs == []


def test_cancel_right_as_the_worker_picks_the_job_up(settings, tmp_path):
    """Regression: a job cancelled while being claimed must never run."""
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    ran = []

    class Recording(JobQueue):
        def process(self, job):
            ran.append(job.id)
            super().process(job)

    queue = Recording(settings, start=False)
    [job] = queue.add([str(clip)], use_gpu=False)
    claimed = queue._claim_next()
    assert claimed is job and job.status == "encoding"
    assert queue.cancel(job.id)  # cancel arrives right after the claim
    queue.process(job)
    assert job.status == "cancelled"
    assert ran == [job.id] and job.out_dir is None, "nothing was encoded"


def test_bad_video_fails_clearly(settings, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    queue = JobQueue(settings)
    try:
        queue.add([str(bad)], use_gpu=False, out_root=tmp_path / "out")
        [job] = wait_finished(queue)
    finally:
        queue.stop()
    assert job["status"] == "failed" and "couldn't read" in job["error"]
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_unwritable_folder_fails_clearly(settings, tmp_path, monkeypatch):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    queue = JobQueue(settings, start=False)
    [job] = queue.add([str(clip)], use_gpu=False, out_root=tmp_path / "out")

    def no_space(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("reelm_encoder.jobs.probe", lambda *_a: _FakeSource())
    monkeypatch.setattr("reelm_encoder.jobs.find_tool", lambda name, _d: name)
    monkeypatch.setattr("reelm_encoder.jobs.new_output_dir", no_space)
    queue.process(job)
    assert job.status == "failed" and "disk full" in (job.error or "")


class _FakeSource:
    width, height, duration = 1280, 720, 10.0


def test_remove_unfinished_only_touches_marked_folders(tmp_path):
    half = tmp_path / "movie-aaaaaa"
    half.mkdir()
    (half / INCOMPLETE).touch()
    (half / "seg_00000.ts").write_bytes(b"x")
    done = tmp_path / "movie-bbbbbb"
    done.mkdir()
    (done / "master.m3u8").write_text("#EXTM3U")
    mine = tmp_path / "Holiday photos"
    mine.mkdir()

    assert remove_unfinished(tmp_path) == ["movie-aaaaaa"]
    assert not half.exists() and done.exists() and mine.exists()
    assert remove_unfinished(tmp_path / "missing") == []


@needs_ffmpeg
def test_encode_command_makes_a_new_folder(settings, tmp_path, capsys, monkeypatch):
    clip = tmp_path / "My Clip.mp4"
    make_clip(clip, seconds=2, size="640x360")
    settings = Settings(_env_file=None, public_url="https://media.example.com/")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    out = tmp_path / "out"
    assert cli.main(["encode", str(clip), "--cpu", "--out", str(out)]) == 0
    [folder] = list(out.iterdir())
    assert folder.name.startswith("my-clip-") and (folder / "master.m3u8").is_file()
    assert not (folder / INCOMPLETE).exists()
    printed = capsys.readouterr().out
    assert f"https://media.example.com/videos/{folder.name}/master.m3u8" in printed
    assert "Duration:  2 seconds" in printed


def test_old_r2_public_url_setting_still_works():
    s = Settings(_env_file=None, r2_public_url="https://pub-abc.r2.dev")
    assert s.video_link("ep-1-abc123") == "https://pub-abc.r2.dev/videos/ep-1-abc123/master.m3u8"
    assert Settings(_env_file=None).video_link("x") is None
