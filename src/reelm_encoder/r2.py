"""Uploading encoded videos to Cloudflare R2 (S3-compatible) and deleting them."""

import re
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from reelm_encoder.config import Settings
from reelm_encoder.encode import CancelledError

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}
# Chunks never change (every video gets a new id): cache them "forever".
SEGMENT_CACHE = "public, max-age=31536000, immutable"
# Playlists are tiny; a short cache leaves room to fix one later (e.g. add subtitles).
PLAYLIST_CACHE = "public, max-age=300"
PREFIX = "videos"
VIDEO_ID = re.compile(r"[0-9a-f]{32}")
URL_ID = re.compile(rf"/{PREFIX}/([0-9a-f]{{32}})/")


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
            # newer boto3 adds checksums R2 doesn't always accept; only send them when needed
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def video_prefix(video_id: str) -> str:
    return f"{PREFIX}/{video_id}/"


def parse_video_id(text: str) -> str:
    """Accept a video id or a stream URL containing it."""
    text = text.strip()
    if VIDEO_ID.fullmatch(text):
        return text
    if match := URL_ID.search(text):
        return match.group(1)
    raise R2Error(f"Invalid video id: {text!r} (expected 32 hex characters, or the stream URL)")


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
        """Fail early with a clear message if the bucket or keys are wrong (read AND write)."""
        from botocore.exceptions import BotoCoreError, ClientError

        probe_key = f"{PREFIX}/.write-check"
        try:
            self.client.list_objects_v2(Bucket=self.bucket, Prefix=f"{PREFIX}/", MaxKeys=1)
            self.client.put_object(Bucket=self.bucket, Key=probe_key, Body=b"ok")
            self.client.delete_object(Bucket=self.bucket, Key=probe_key)
        except (ClientError, BotoCoreError) as e:
            raise R2Error(f"Can't use bucket '{self.bucket}': {e}") from e

    def _put(self, path: Path, key: str) -> None:
        playlist = path.suffix.lower() == ".m3u8"
        extra = {
            "ContentType": CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            "CacheControl": PLAYLIST_CACHE if playlist else SEGMENT_CACHE,
        }
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)

    def upload_video(
        self,
        local_dir: Path,
        video_id: str,
        on_progress: Callable[[int, int], None] = lambda _done, _total: None,
        workers: int = 8,
        cancel: threading.Event | None = None,
    ) -> str:
        """Upload a folder of HLS files; returns the public master playlist URL.

        Chunks go first and playlists last, so the link never points at
        missing files. If anything fails, what was uploaded is removed again.
        """
        prefix = video_prefix(parse_video_id(video_id))
        files = sorted(p for p in local_dir.rglob("*") if p.is_file())
        if not any(p.name == "master.m3u8" for p in files):
            raise R2Error(f"No master.m3u8 in {local_dir}")
        playlists = sorted(
            (p for p in files if p.suffix == ".m3u8"), key=lambda p: p.name == "master.m3u8"
        )  # variant playlists first, the master last
        segments = [p for p in files if p.suffix != ".m3u8"]
        total, done = len(files), 0

        def key_for(p: Path) -> str:
            return prefix + p.relative_to(local_dir).as_posix()

        def check_cancel() -> None:
            if cancel is not None and cancel.is_set():
                raise CancelledError

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            pending: set[Future] = {pool.submit(self._put, p, key_for(p)) for p in segments}
            while pending:
                # short waits so Ctrl+C / Cancel are noticed quickly
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_EXCEPTION)
                for future in finished:
                    future.result()  # raises the first upload error
                    done += 1
                    on_progress(done, total)
                check_cancel()
            pool.shutdown()
            for p in playlists:
                check_cancel()
                self._put(p, key_for(p))
                done += 1
                on_progress(done, total)
        except BaseException as e:
            # stop queued uploads and wait for running ones BEFORE cleaning up,
            # so nothing lands in the bucket after the cleanup
            pool.shutdown(wait=True, cancel_futures=True)
            cleanup_note = ""
            try:
                self.delete_video(video_id)
            except Exception as cleanup_error:
                cleanup_note = (
                    f" Some files may remain in R2 ({cleanup_error}); remove them with: "
                    f"reelm-encoder delete {video_id}"
                )
            if isinstance(e, CancelledError | KeyboardInterrupt):
                raise
            if isinstance(e, R2Error):
                raise R2Error(f"{e}{cleanup_note}") from e
            raise R2Error(f"Upload failed: {e}.{cleanup_note}") from e
        return self.url(prefix + "master.m3u8")

    def delete_video(self, video_id: str) -> int:
        """Delete every file of one video. Returns how many were deleted."""
        prefix = video_prefix(parse_video_id(video_id))
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
