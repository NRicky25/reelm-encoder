import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from reelm_encoder.config import Settings
from reelm_encoder.encode import CancelledError
from reelm_encoder.r2 import R2, R2Error, make_client, parse_video_id

VID = "0123456789abcdef0123456789abcdef"
OTHER = "0123456789abcdef0123456789abcdee"


class FakeS3:
    """Just enough of boto3's S3 client for R2 (records uploads, in order)."""

    def __init__(self, fail_on: str | None = None, fail_delete: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.order: list[str] = []
        self.fail_on = fail_on
        self.fail_delete = fail_delete
        self.lock = threading.Lock()

    def upload_file(self, filename, bucket, key, ExtraArgs):  # noqa: N803 (boto3 name)
        assert bucket == "reelm-videos"
        if self.fail_on and key.endswith(self.fail_on):
            raise OSError("network down")
        with self.lock:
            self.objects[key] = {"body": Path(filename).read_bytes(), **ExtraArgs}
            self.order.append(key)

    def list_objects_v2(self, **_kw):
        return {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[Key] = {"body": Body}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                if fake.fail_delete:
                    raise OSError("R2 unreachable")
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 2):  # small pages
                    yield {"Contents": [{"Key": k} for k in keys[i : i + 2]]}

        return Paginator()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {}


def hls_folder(tmp_path: Path, segments: int = 3) -> Path:
    out = tmp_path / "out"
    for v in ("v720p", "v480p"):
        (out / v).mkdir(parents=True)
        (out / v / "index.m3u8").write_text("#EXTM3U\n")
        for i in range(segments):
            (out / v / f"seg_{i:05d}.ts").write_bytes(b"ts")
    (out / "master.m3u8").write_text("#EXTM3U\n")
    return out


def r2(fake) -> R2:
    return R2(fake, "reelm-videos", "https://pub-abc.r2.dev/")


def test_upload_order_types_and_url(tmp_path):
    fake = FakeS3()
    url = r2(fake).upload_video(hls_folder(tmp_path), VID)
    assert url == f"https://pub-abc.r2.dev/videos/{VID}/master.m3u8"
    assert len(fake.objects) == 9
    assert fake.order[-1] == f"videos/{VID}/master.m3u8", "master goes last"
    assert all(k.endswith(".ts") for k in fake.order[:6]), "chunks before playlists"
    seg = fake.objects[f"videos/{VID}/v720p/seg_00000.ts"]
    assert seg["ContentType"] == "video/mp2t" and "immutable" in seg["CacheControl"]
    master = fake.objects[f"videos/{VID}/master.m3u8"]
    assert master["ContentType"] == "application/vnd.apple.mpegurl"
    assert "immutable" not in master["CacheControl"], "playlists get a short cache"


@pytest.mark.parametrize("fail_on", ["v480p/index.m3u8", "v720p/seg_00001.ts"])
def test_failed_upload_cleans_up(tmp_path, fail_on):
    """Failures in the parallel chunk phase and in the playlist phase both clean up."""
    fake = FakeS3(fail_on=fail_on)
    with pytest.raises(R2Error, match="Upload failed"):
        r2(fake).upload_video(hls_folder(tmp_path, segments=20), VID)
    assert fake.objects == {}, "half-uploaded video removed"


def test_failed_cleanup_is_reported(tmp_path):
    fake = FakeS3(fail_on="seg_00001.ts", fail_delete=True)
    with pytest.raises(R2Error) as e:
        r2(fake).upload_video(hls_folder(tmp_path), VID)
    assert f"reelm-encoder delete {VID}" in str(e.value)


def test_cancel_stops_and_cleans_up(tmp_path):
    fake = FakeS3()
    cancel = threading.Event()

    def progress(done, _total):
        if done == 3:
            cancel.set()

    with pytest.raises(CancelledError):
        r2(fake).upload_video(hls_folder(tmp_path, segments=30), VID, progress, cancel=cancel)
    assert fake.objects == {}


def test_delete_only_that_video(tmp_path):
    fake = FakeS3()
    store = r2(fake)
    store.upload_video(hls_folder(tmp_path), VID)
    fake.objects[f"videos/{OTHER}/master.m3u8"] = {}  # another video, must survive
    assert store.delete_video(VID) == 9
    assert list(fake.objects) == [f"videos/{OTHER}/master.m3u8"]
    for bad in ("", "../x", "a/b", "..", "abc123", VID + "0"):
        with pytest.raises(R2Error, match="Invalid"):
            store.delete_video(bad)


def test_video_id_from_url():
    assert parse_video_id(f"https://pub-abc.r2.dev/videos/{VID}/master.m3u8") == VID
    assert parse_video_id(f"  {VID} ") == VID


def test_check_reports_access_problems_without_the_secret():
    class Denied(FakeS3):
        def put_object(self, **_kw):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")

    with pytest.raises(R2Error, match="Can't use bucket") as e:
        r2(Denied()).check()
    assert "secret" not in str(e.value)


def test_check_leaves_nothing_behind():
    fake = FakeS3()
    r2(fake).check()
    assert fake.objects == {}


def test_missing_settings_are_named():
    s = Settings(_env_file=None, r2_account_id="acct", r2_public_url="")
    with pytest.raises(R2Error) as e:
        make_client(s)
    assert "R2_ACCESS_KEY_ID" in str(e.value) and "R2_PUBLIC_URL" in str(e.value)
    assert "R2_ACCOUNT_ID" not in str(e.value)


def test_secret_is_never_shown():
    s = Settings(_env_file=None, r2_secret_access_key="super-secret-value")
    assert "super-secret-value" not in repr(s)
