"""Uploading encoded videos to Cloudflare R2 (S3-compatible) and deleting them."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from reelm_encoder.config import Settings

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
    ".jpg": "image/jpeg",
}
# Every video lives under its own new id, so files never change: cache "forever".
CACHE_CONTROL = "public, max-age=31536000, immutable"
PREFIX = "videos"


class R2Error(RuntimeError):
    pass


def make_client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    if not settings.r2_ready:
        raise R2Error(f"R2 isn't configured: set {', '.join(settings.missing_r2())} in .env")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),  # type: ignore[union-attr]
        region_name="auto",
        config=Config(
            retries={"max_attempts": 6, "mode": "standard"},
            max_pool_connections=16,
        ),
    )


def video_prefix(video_id: str) -> str:
    return f"{PREFIX}/{video_id}/"


class R2:
    def __init__(self, client: Any, bucket: str, public_url: str) -> None:
        self.client = client
        self.bucket = bucket
        self.public_url = public_url.rstrip("/")

    @classmethod
    def from_settings(cls, settings: Settings) -> "R2":
        return cls(make_client(settings), settings.r2_bucket, settings.r2_public_url or "")

    def url(self, key: str) -> str:
        return f"{self.public_url}/{key}"

    def check(self) -> None:
        """Fail early with a clear message if the bucket or keys are wrong."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as e:
            raise R2Error(f"Can't access bucket '{self.bucket}': {e}") from e

    def _put(self, path: Path, key: str) -> None:
        extra = {
            "ContentType": CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            "CacheControl": CACHE_CONTROL,
        }
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)

    def upload_video(
        self,
        local_dir: Path,
        video_id: str,
        on_progress: Callable[[int, int], None] = lambda _done, _total: None,
        workers: int = 8,
    ) -> str:
        """Upload a folder of HLS files; returns the public master playlist URL.

        Chunks go first and playlists last, so the link never points at
        missing files. If anything fails, what was uploaded is removed again.
        """
        prefix = video_prefix(video_id)
        files = sorted(p for p in local_dir.rglob("*") if p.is_file())
        if not any(p.name == "master.m3u8" for p in files):
            raise R2Error(f"No master.m3u8 in {local_dir}")
        playlists = [p for p in files if p.suffix == ".m3u8"]
        # variant playlists before the master
        playlists.sort(key=lambda p: p.name == "master.m3u8")
        segments = [p for p in files if p.suffix != ".m3u8"]
        total, done = len(files), 0

        def key_for(p: Path) -> str:
            return prefix + p.relative_to(local_dir).as_posix()

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(self._put, p, key_for(p)) for p in segments]
                for future in as_completed(futures):
                    future.result()
                    done += 1
                    on_progress(done, total)
            for p in playlists:
                self._put(p, key_for(p))
                done += 1
                on_progress(done, total)
        except BaseException as e:
            try:
                self.delete_video(video_id)
            except Exception:
                pass
            if isinstance(e, Exception) and not isinstance(e, R2Error):
                raise R2Error(f"Upload failed: {e}") from e
            raise
        return self.url(prefix + "master.m3u8")

    def delete_video(self, video_id: str) -> int:
        """Delete every file of one video. Returns how many were deleted."""
        if not video_id or "/" in video_id or video_id in (".", ".."):
            raise R2Error(f"Invalid video id: {video_id!r}")
        prefix = video_prefix(video_id)
        deleted = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                batch = keys[i : i + 1000]
                result = self.client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
                )
                if errors := result.get("Errors"):
                    raise R2Error(f"Couldn't delete {len(errors)} files, e.g. {errors[0]}")
                deleted += len(batch)
        return deleted
