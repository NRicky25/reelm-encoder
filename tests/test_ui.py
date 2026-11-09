import threading
import time

import pytest
from fastapi.testclient import TestClient

from reelm_encoder.config import Settings
from reelm_encoder.jobs import INCOMPLETE, PENDING_UPLOADS, JobQueue
from reelm_encoder.r2 import R2
from reelm_encoder.ui import ACTION_HEADER, PickerError, create_app
from tests.test_encode import make_clip, needs_ffmpeg
from tests.test_r2 import VID, FakeS3

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


@pytest.fixture
def fake_s3() -> FakeS3:
    return FakeS3()


def make_ui(settings, fake_s3, *, picker=lambda: [], queue=None):
    queue = queue or JobQueue(settings, r2_factory=lambda _s: FakeR2(fake_s3))
    app = create_app(settings, queue, picker=picker, allowed_hosts={HOST, "localhost:8800"})
    client = TestClient(app, base_url=f"http://{HOST}", headers=ACT)
    return client, queue


@pytest.fixture
def ui(settings, fake_s3):
    client, queue = make_ui(settings, fake_s3)
    yield client
    queue.stop()


def wait_for(client, predicate, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        jobs = client.get("/api/jobs").json()
        if jobs and predicate(jobs):
            return jobs
        time.sleep(0.05)
    raise AssertionError(f"timed out: {client.get('/api/jobs').json()}")


def all_finished(client, timeout=60):
    done = {"done", "failed", "cancelled"}
    return wait_for(client, lambda js: all(j["status"] in done for j in js), timeout)


# ───────── security ─────────
def test_page_loads_without_the_header(ui):
    r = ui.get("/", headers={ACTION_HEADER: ""})
    assert r.status_code == 200 and "Reelm Encoder" in r.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/jobs"),
        ("GET", "/api/status"),
        ("POST", "/api/status/refresh"),
        ("POST", "/api/pick"),
        ("POST", "/api/jobs"),
        ("POST", "/api/jobs/x/cancel"),
        ("POST", "/api/jobs/x/remove"),
        ("POST", "/api/jobs/x/open-folder"),
        ("POST", "/api/jobs/x/delete-from-r2"),
        ("OPTIONS", "/api/jobs"),
        ("HEAD", "/api/jobs"),
    ],
)
def test_every_api_call_needs_the_custom_header(ui, method, path):
    """Other websites can't add the header, so they can't use the API (not even reads)."""
    r = ui.request(method, path, headers={ACTION_HEADER: ""})
    assert r.status_code == 403


def test_other_hostnames_are_refused(ui):
    """DNS rebinding: evil.com resolving to 127.0.0.1 still sends Host: evil.com."""
    assert ui.get("/api/jobs", headers={"host": "evil.example:8800"}).status_code == 403
    assert ui.get("/", headers={"host": "127.0.0.1"}).status_code == 403
    assert ui.get("/api/jobs", headers={"host": "localhost:8800"}).status_code == 200


def test_default_is_to_refuse_unknown_hosts(settings, fake_s3):
    queue = JobQueue(settings, start=False)
    app = create_app(settings, queue)  # no allowed_hosts given: only 127.0.0.1:8800/localhost
    client = TestClient(app, base_url="http://testserver", headers=ACT)
    assert client.get("/api/jobs").status_code == 403


# ───────── inputs ─────────
def test_missing_file_is_refused(ui, tmp_path):
    r = ui.post("/api/jobs", json={"paths": [str(tmp_path / "nope.mp4")]})
    assert r.status_code == 400 and "not found" in r.text
    assert ui.post("/api/jobs", json={"paths": []}).status_code == 422


def test_open_folder(ui, tmp_path, monkeypatch, settings):
    assert ui.post("/api/jobs/nope/open-folder").status_code == 404
    opened = []
    monkeypatch.setattr("os.startfile", opened.append, raising=False)
    queue: JobQueue = ui.app.state.queue
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    [job] = queue.add([str(clip)], upload=False, use_gpu=False)
    all_finished(ui)  # fails (not a video); give it a folder to open
    job.out_dir = settings.work_dir / "x"
    job.out_dir.mkdir(parents=True)
    assert ui.post(f"/api/jobs/{job.id}/open-folder").json() == {"ok": True}
    assert opened == [job.out_dir]


def test_picker_errors_are_shown(settings, fake_s3):
    def broken():
        raise PickerError("The file picker failed: no display")

    client, queue = make_ui(settings, fake_s3, picker=broken)
    r = client.post("/api/pick")
    assert r.status_code == 500 and "no display" in r.text
    queue.stop()


def test_only_one_picker_at_a_time(settings, fake_s3):
    release = threading.Event()

    def slow():
        release.wait(5)
        return []

    client, queue = make_ui(settings, fake_s3, picker=slow)
    first = threading.Thread(target=lambda: client.post("/api/pick"))
    first.start()
    time.sleep(0.2)
    assert client.post("/api/pick").status_code == 409
    release.set()
    first.join()
    queue.stop()


# ───────── status ─────────
def test_status_reports_r2(ui):
    data = ui.get("/api/status").json()
    assert data["r2"]["configured"] and data["r2"]["reachable"]
    assert "ffmpeg" in data and data["notes"] == []


def test_status_without_r2(tmp_path, fake_s3):
    s = Settings(_env_file=None, work_dir=tmp_path)
    client, queue = make_ui(s, fake_s3)
    r2 = client.get("/api/status").json()["r2"]
    assert r2["configured"] is False and "R2_ACCOUNT_ID" in r2["missing"]
    queue.stop()


# ───────── jobs ─────────
def test_queued_job_can_be_cancelled(settings, tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    queue = JobQueue(settings, start=False)  # no worker: the job stays queued
    [job] = queue.add([str(clip)], upload=False, use_gpu=False)
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
    [job] = queue.add([str(clip)], upload=False, use_gpu=False)
    claimed = queue._claim_next()
    assert claimed is job and job.status == "encoding"
    assert queue.cancel(job.id)  # cancel arrives right after the claim
    queue.process(job)
    assert job.status == "cancelled"
    assert ran == [job.id] and job.out_dir is None, "nothing was encoded"


def test_bad_video_fails_clearly(ui, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    ui.post("/api/jobs", json={"paths": [str(bad)]})
    [job] = all_finished(ui)
    assert job["status"] == "failed" and "couldn't read" in job["error"]


@needs_ffmpeg
def test_encode_and_upload_then_delete(ui, tmp_path, fake_s3):
    clip = tmp_path / "my episode.mp4"
    make_clip(clip, seconds=3)
    assert ui.post("/api/jobs", json={"paths": [str(clip)], "gpu": False}).status_code == 200
    [job] = all_finished(ui)
    assert job["status"] == "done", job
    assert job["url"] == f"https://pub-abc.r2.dev/videos/{job['videoId']}/master.m3u8"
    assert job["duration"] == 3 and job["qualities"] == ["720p", "480p"]
    assert f"videos/{job['videoId']}/master.m3u8" in fake_s3.objects
    assert job["folder"] is None, "local copy removed after upload"

    r = ui.post(f"/api/jobs/{job['id']}/delete-from-r2")
    assert r.json()["deleted"] > 0 and not fake_s3.objects
    assert ui.get("/api/jobs").json() == [], "removed from the list too"


@needs_ffmpeg
def test_encode_only_keeps_the_files(ui, tmp_path, fake_s3):
    clip = tmp_path / "clip.mp4"
    make_clip(clip, seconds=2, size="640x360")
    ui.post("/api/jobs", json={"paths": [str(clip)], "upload": False, "gpu": False})
    [job] = all_finished(ui)
    assert job["status"] == "done" and job["url"] is None and job["folder"]
    assert fake_s3.objects == {}
    assert ui.post(f"/api/jobs/{job['id']}/delete-from-r2").status_code == 404


@needs_ffmpeg
def test_cancel_a_running_encode(ui, tmp_path, settings):
    clip = tmp_path / "long.mp4"
    make_clip(clip, seconds=60, size="1920x1080")
    ui.post("/api/jobs", json={"paths": [str(clip)], "upload": False, "gpu": False})
    [job] = wait_for(ui, lambda js: js[0]["status"] == "encoding" and js[0]["progress"] > 0)
    assert ui.post(f"/api/jobs/{job['id']}/cancel").json() == {"ok": True}
    [job] = all_finished(ui, timeout=15)
    assert job["status"] == "cancelled" and job["folder"] is None
    assert not (settings.work_dir / job["videoId"]).exists(), "partial files removed"


@needs_ffmpeg
def test_failed_upload_keeps_the_encode_and_cleans_r2(settings, tmp_path):
    fake = FakeS3(fail_on="seg_00000.ts")
    client, queue = make_ui(settings, fake)
    clip = tmp_path / "clip.mp4"
    make_clip(clip, seconds=3)
    client.post("/api/jobs", json={"paths": [str(clip)], "gpu": False})
    [job] = all_finished(client)
    assert job["status"] == "failed" and "encoded files kept" in job["message"]
    assert job["folder"] and fake.objects == {}
    assert not (settings.work_dir / PENDING_UPLOADS).exists()
    queue.stop()


# ───────── after a crash ─────────
def test_recover_cleans_up_after_a_crash(settings, fake_s3):
    half = settings.work_dir / ("a" * 32)
    half.mkdir(parents=True)
    (half / INCOMPLETE).touch()
    kept = settings.work_dir / ("b" * 32)  # a finished encode-only result
    kept.mkdir()
    fake_s3.objects[f"videos/{VID}/v720p/seg_00000.ts"] = {}
    (settings.work_dir / PENDING_UPLOADS).write_text(VID + "\n")

    queue = JobQueue(settings, r2_factory=lambda _s: FakeR2(fake_s3), start=False)
    notes = queue.recover()
    assert not half.exists() and kept.exists()
    assert fake_s3.objects == {} and not (settings.work_dir / PENDING_UPLOADS).exists()
    assert len(notes) == 2
