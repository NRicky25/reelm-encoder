"""A queue of encode (+ upload) jobs, processed one at a time in a background thread.

Used by the web interface; the command line runs the same steps directly.
"""

import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reelm_encoder.config import Settings
from reelm_encoder.encode import CancelledError, EncodeError, build_command, encode
from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, probe
from reelm_encoder.r2 import R2, R2Error
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

Status = Literal["queued", "encoding", "uploading", "done", "failed", "cancelled"]
FINISHED: set[str] = {"done", "failed", "cancelled"}


@dataclass
class Job:
    path: Path
    upload: bool
    use_gpu: bool
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = "queued"
    progress: float = 0.0  # of the current stage, 0..1
    message: str = "Waiting…"
    eta_seconds: float | None = None
    # filled in as the job runs
    size: str | None = None
    duration: float | None = None
    qualities: list[str] = field(default_factory=list)
    gpu: bool | None = None
    video_id: str | None = None
    url: str | None = None
    out_dir: Path | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.path.name,
            "path": str(self.path),
            "upload": self.upload,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "eta": None if self.eta_seconds is None else round(self.eta_seconds),
            "size": self.size,
            "duration": None if self.duration is None else round(self.duration),
            "qualities": self.qualities,
            "gpu": self.gpu,
            "videoId": self.video_id,
            "url": self.url,
            "folder": str(self.out_dir) if self.out_dir and self.out_dir.exists() else None,
            "error": self.error,
        }


class JobQueue:
    def __init__(
        self,
        settings: Settings,
        r2_factory: Callable[[Settings], R2] = R2.from_settings,
        *,
        start: bool = True,
    ):
        self.settings = settings
        self.r2_factory = r2_factory
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        if start:
            threading.Thread(target=self._run, daemon=True, name="encoder").start()

    # ── public API (called from web requests) ──
    def add(self, paths: list[str], *, upload: bool, use_gpu: bool) -> list[Job]:
        new = [Job(Path(p), upload=upload, use_gpu=use_gpu) for p in paths]
        with self._lock:
            self.jobs.extend(new)
        self._wake.set()
        return new

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self.jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in FINISHED:
            return False
        job.cancel.set()
        if job.status == "queued":
            self._finish(job, "cancelled", "Cancelled")
        return True

    def remove(self, job_id: str) -> bool:
        """Forget a finished job (encoded files of encode-only jobs stay on disk)."""
        with self._lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None or job.status not in FINISHED:
                return False
            self.jobs.remove(job)
            return True

    def busy(self) -> bool:
        with self._lock:
            return any(j.status not in FINISHED for j in self.jobs)

    # ── worker ──
    def _next(self) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.status == "queued"), None)

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                self.process(job)
            except Exception as e:  # never let one bad job stop the queue
                self._finish(job, "failed", "Failed", error=f"Unexpected error: {e}")

    def _finish(self, job: Job, status: Status, message: str, error: str | None = None) -> None:
        job.status, job.message, job.error, job.eta_seconds = status, message, error, None

    def process(self, job: Job) -> None:
        s = self.settings
        job.status, job.message, job.progress = "encoding", "Reading video…", 0.0
        try:
            ffmpeg = find_tool("ffmpeg", s.ffmpeg_dir)
            ffprobe = find_tool("ffprobe", s.ffmpeg_dir)
            r2 = self.r2_factory(s) if job.upload else None
            if r2 is not None:
                r2.check()  # fail before spending minutes encoding
            source = probe(ffprobe, job.path)
        except (ToolMissingError, ProbeError, R2Error) as e:
            return self._finish(job, "failed", "Failed", error=str(e))

        rungs = pick_rungs(source.width, source.height)
        job.size = f"{source.width}x{source.height}"
        job.duration = source.duration
        job.qualities = [r.label for r in rungs]
        job.gpu = job.use_gpu and nvenc_available(ffmpeg)
        job.video_id = uuid.uuid4().hex
        job.out_dir = s.work_dir / job.video_id
        job.message = "Encoding"
        started = time.monotonic()

        def on_encode(fraction: float) -> None:
            job.progress = fraction
            elapsed = time.monotonic() - started
            job.eta_seconds = elapsed / fraction - elapsed if 0.02 < fraction < 1 else None

        try:
            command = build_command(ffmpeg, source, rungs, gpu=job.gpu)
            encode(
                command,
                job.out_dir,
                source.duration,
                on_encode,
                cancel=job.cancel,
                expected_variants=len(rungs),
            )
        except CancelledError:
            shutil.rmtree(job.out_dir, ignore_errors=True)
            return self._finish(job, "cancelled", "Cancelled")
        except EncodeError as e:
            shutil.rmtree(job.out_dir, ignore_errors=True)
            return self._finish(job, "failed", "Encoding failed", error=str(e))

        if r2 is None:
            job.progress = 1.0
            return self._finish(job, "done", "Encoded (not uploaded)")

        job.status, job.message, job.progress, job.eta_seconds = "uploading", "Uploading", 0, None

        def on_upload(done: int, total: int) -> None:
            job.progress = done / total

        try:
            job.url = r2.upload_video(job.out_dir, job.video_id, on_upload, cancel=job.cancel)
        except CancelledError:
            return self._finish(job, "cancelled", "Cancelled (encoded files kept)")
        except R2Error as e:
            return self._finish(job, "failed", "Upload failed (encoded files kept)", error=str(e))
        shutil.rmtree(job.out_dir, ignore_errors=True)
        job.progress = 1.0
        self._finish(job, "done", "Ready")
