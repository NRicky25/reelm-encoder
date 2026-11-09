from pathlib import Path

import pytest

from reelm_encoder.config import Settings
from reelm_encoder.r2 import R2, R2Error, make_client


class FakeS3:
    """Just enough of boto3's S3 client for R2 (records uploads, in order)."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.objects: dict[str, dict] = {}
        self.order: list[str] = []
        self.fail_on = fail_on

    def upload_file(self, filename, bucket, key, ExtraArgs):  # noqa: N803 (boto3 name)
        assert bucket == "reelm-videos"
        if self.fail_on and key.endswith(self.fail_on):
            raise OSError("network down")
        self.objects[key] = {"body": Path(filename).read_bytes(), **ExtraArgs}
        self.order.append(key)

    def get_paginator(self, _name):
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 2):  # small pages
                    yield {"Contents": [{"Key": k} for k in keys[i : i + 2]]}

        return Paginator()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {}


def hls_folder(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    for v in ("v0", "v1"):
        (out / v).mkdir(parents=True)
        (out / v / "index.m3u8").write_text("#EXTM3U\n")
        for i in range(3):
            (out / v / f"seg_{i:05d}.ts").write_bytes(b"ts")
    (out / "master.m3u8").write_text("#EXTM3U\n")
    return out


def r2(fake) -> R2:
    return R2(fake, "reelm-videos", "https://pub-abc.r2.dev/")


def test_upload_order_types_and_url(tmp_path):
    fake = FakeS3()
    url = r2(fake).upload_video(hls_folder(tmp_path), "abc123")
    assert url == "https://pub-abc.r2.dev/videos/abc123/master.m3u8"
    assert len(fake.objects) == 9
    assert fake.order[-1] == "videos/abc123/master.m3u8", "master goes last"
    assert all(k.endswith(".ts") for k in fake.order[:6]), "chunks before playlists"
    seg = fake.objects["videos/abc123/v0/seg_00000.ts"]
    assert seg["ContentType"] == "video/mp2t" and "immutable" in seg["CacheControl"]
    assert (
        fake.objects["videos/abc123/master.m3u8"]["ContentType"] == "application/vnd.apple.mpegurl"
    )


def test_failed_upload_cleans_up(tmp_path):
    fake = FakeS3(fail_on="v1/index.m3u8")
    with pytest.raises(R2Error, match="Upload failed"):
        r2(fake).upload_video(hls_folder(tmp_path), "abc123")
    assert fake.objects == {}, "half-uploaded video removed"


def test_delete_only_that_video(tmp_path):
    fake = FakeS3()
    store = r2(fake)
    store.upload_video(hls_folder(tmp_path), "abc123")
    fake.objects["videos/abc1234/master.m3u8"] = {}  # similar id, must survive
    assert store.delete_video("abc123") == 9
    assert list(fake.objects) == ["videos/abc1234/master.m3u8"]
    for bad in ("", "../x", "a/b", ".."):
        with pytest.raises(R2Error, match="Invalid"):
            store.delete_video(bad)


def test_missing_settings_are_named():
    s = Settings(_env_file=None, r2_account_id="acct", r2_public_url="")
    with pytest.raises(R2Error) as e:
        make_client(s)
    assert "R2_ACCESS_KEY_ID" in str(e.value) and "R2_PUBLIC_URL" in str(e.value)
    assert "R2_ACCOUNT_ID" not in str(e.value)
