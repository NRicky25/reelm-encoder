import time

import pytest
from fastapi.testclient import TestClient

from reelm_encoder.config import Settings
from reelm_encoder.jobs import JobQueue
from reelm_encoder.r2 import R2
from reelm_encoder.ui import ACTION_HEADER, create_app
from tests.test_encode import make_clip, needs_ffmpeg
from tests.test_r2 import FakeS3

HOST = "127.0.0.1:8800"
ACT = {ACTION_HEADER: "1"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        work_dir=tmp_path / "work",
        r2_account_id="acct",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_public_url="https://pub-abc.r2.dev",
    )


class FakeR2(R2):
    def __init__(self, fake: FakeS3) -> None:
        super().__init__(fake, "reelm-videos", "https://pub-abc.r2.dev")

    def check(self) -> None:
        pass


@pytest.fixture
def fake_s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def ui(settings, fake_s3, tmp_path):
    picked: list[str] = []
    queue = JobQueue(settings, r2_factory=lambda _s: FakeR2(fake_s3))
    app = create_app(
        settings, queue, picker=lambda: list(picked), allowed_hosts={HOST, "localhost:8800"}
    )
    client = TestClient(app, base_url=f"http://{HOST}")
    client.picked = picked  # type: ignore[attr-defined]
    return client


def wait_until_finished(client, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        jobs = client.get("/api/jobs").json()
        if jobs and all(j["status"] in ("done", "failed", "cancelled") for j in jobs):
            return jobs
        time.sleep(0.1)
    raise AssertionError(f"jobs didn't finish: {client.get('/api/jobs').json()}")


def test_page_loads(ui):
    r = ui.get("/")
    assert r.status_code == 200 and "Reelm Encoder" in r.text


def test_actions_need_the_custom_header(ui):
    """Another website can't make the browser POST here (it can't add the header)."""
    assert ui.post("/api/pick").status_code == 403
    assert ui.post("/api/pick", headers=ACT).status_code == 200


def test_other_hostnames_are_refused(ui):
    """DNS rebinding: evil.com resolving to 127.0.0.1 still sends Host: evil.com."""
    r = ui.get("/api/jobs", headers={"host": "evil.example:8800"})
    assert r.status_code == 403
    assert ui.get("/api/jobs", headers={"host": "localhost:8800"}).status_code == 200


def test_missing_file_is_refused(ui, tmp_path):
    r = ui.post("/api/jobs", json={"paths": [str(tmp_path / "nope.mp4")]}, headers=ACT)
    assert r.status_code == 400 and "not found" in r.text


def test_delete_needs_a_real_video_id(ui):
    for bad in ("../x", "abc", "A" * 32):
        r = ui.post("/api/delete", json={"video_id": bad}, headers=ACT)
        assert r.status_code == 422


@needs_ffmpeg
def test_encode_and_upload_through_the_ui(ui, tmp_path, fake_s3):
    clip = tmp_path / "my episode.mp4"
    make_clip(clip, seconds=3)
    r = ui.post("/api/jobs", json={"paths": [str(clip)], "gpu": False}, headers=ACT)
    assert r.status_code == 200
    [job] = wait_until_finished(ui)
    assert job["status"] == "done", job
    assert job["url"] == f"https://pub-abc.r2.dev/videos/{job['videoId']}/master.m3u8"
    assert job["duration"] == 3 and job["qualities"] == ["720p", "480p"]
    assert f"videos/{job['videoId']}/master.m3u8" in fake_s3.objects
    assert job["folder"] is None, "local copy removed after upload"

    r = ui.post("/api/delete", json={"video_id": job["videoId"]}, headers=ACT)
    assert r.json()["deleted"] > 0 and not fake_s3.objects
    assert ui.post(f"/api/jobs/{job['id']}/remove", headers=ACT).json() == {"ok": True}
    assert ui.get("/api/jobs").json() == []


@needs_ffmpeg
def test_encode_only_keeps_the_files(ui, tmp_path, fake_s3):
    clip = tmp_path / "clip.mp4"
    make_clip(clip, seconds=2, size="640x360")
    ui.post("/api/jobs", json={"paths": [str(clip)], "upload": False, "gpu": False}, headers=ACT)
    [job] = wait_until_finished(ui)
    assert job["status"] == "done" and job["url"] is None and job["folder"]
    assert fake_s3.objects == {}


def test_queued_job_can_be_cancelled(settings, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    queue = JobQueue(settings, start=False)  # no worker: the job stays queued
    [job] = queue.add([str(clip)], upload=False, use_gpu=False)
    assert queue.cancel(job.id) and job.status == "cancelled"
    assert not queue.cancel(job.id), "already finished"
    assert queue.remove(job.id) and queue.jobs == []


def test_bad_video_fails_clearly(ui, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    ui.post("/api/jobs", json={"paths": [str(bad)]}, headers=ACT)
    [job] = wait_until_finished(ui)
    assert job["status"] == "failed" and "couldn't read" in job["error"]
