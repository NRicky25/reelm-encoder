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


INCOMPLETE = ".incomplete"  # marker file: this folder's encode never finished
PENDING_UPLOADS = "pending-uploads.txt"  # video ids whose upload never finished


class JobQueue:
    """Jobs run one at a time. Every status change happens under one lock, so a
    job cancelled while queued can never start, and the page always sees a
    consistent state."""

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
        self._stopping = False
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True, name="encoder")
            self._thread.start()

    # ── public API (called from web requests) ──
    def add(self, paths: list[str], *, upload: bool, use_gpu: bool) -> list[Job]:
        new = [Job(Path(p), upload=upload, use_gpu=use_gpu) for p in paths]
        with self._lock:
            self.jobs.extend(new)
        self._wake.set()
        return new

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self.jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None or job.status in FINISHED:
                return False
            job.cancel.set()  # a running job notices this and stops
            if job.status == "queued":
                job.status, job.message = "cancelled", "Cancelled"
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

    def stop(self, timeout: float = 10) -> None:
        """Stop the worker after the current job (used by tests)."""
        self._stopping = True
        with self._lock:
            for job in self.jobs:
                job.cancel.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ── left over from a window closed mid-job ──
    def recover(self) -> list[str]:
        """Delete half-finished encodes, and half-uploaded videos in R2. Returns notes."""
        notes: list[str] = []
        work = self.settings.work_dir
        if work.is_dir():
            for marker in work.glob(f"*/{INCOMPLETE}"):
                shutil.rmtree(marker.parent, ignore_errors=True)
                notes.append(f"Removed an unfinished encode ({marker.parent.name})")
        pending = self._pending_ids()
        if pending and self.settings.r2_ready:
            try:
                r2 = self.r2_factory(self.settings)
                for video_id in pending:
                    if r2.delete_video(video_id):
                        notes.append(f"Removed a half-uploaded video from R2 ({video_id})")
                    self._unmark_pending(video_id)
            except R2Error as e:
                notes.append(f"Couldn't clean up half-uploaded videos yet: {e}")
        return notes

    def _pending_file(self) -> Path:
        return self.settings.work_dir / PENDING_UPLOADS

    def _pending_ids(self) -> list[str]:
        try:
            return [x for x in self._pending_file().read_text().split() if x]
        except OSError:
            return []

    def _mark_pending(self, video_id: str) -> None:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        with self._pending_file().open("a") as f:
            f.write(video_id + "\n")

    def _unmark_pending(self, video_id: str) -> None:
        rest = [x for x in self._pending_ids() if x != video_id]
        try:
            if rest:
                self._pending_file().write_text("\n".join(rest) + "\n")
            else:
                self._pending_file().unlink(missing_ok=True)
        except OSError:
            pass

    # ── worker ──
    def _claim_next(self) -> Job | None:
        """Take the next queued job and mark it started, in one step."""
        with self._lock:
            job = next((j for j in self.jobs if j.status == "queued"), None)
            if job is not None:
                job.status, job.message, job.progress = "encoding", "Reading video…", 0.0
            return job

    def _run(self) -> None:
        while not self._stopping:
            job = self._claim_next()
            if job is None:
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                self.process(job)
            except BaseException as e:  # never let one bad job stop the queue
                if job.out_dir is not None:
                    shutil.rmtree(job.out_dir, ignore_errors=True)
                self._finish(job, "failed", "Failed", error=f"Unexpected error: {e!r}")
                if not isinstance(e, Exception):
                    raise

    def _finish(self, job: Job, status: Status, message: str, error: str | None = None) -> None:
        with self._lock:
            job.status, job.message, job.error, job.eta_seconds = status, message, error, None

    def _set(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)

    def process(self, job: Job) -> None:
        s = self.settings
        if job.cancel.is_set():
            return self._finish(job, "cancelled", "Cancelled")
        try:
            ffmpeg = find_tool("ffmpeg", s.ffmpeg_dir)
            ffprobe = find_tool("ffprobe", s.ffmpeg_dir)
            r2 = self.r2_factory(s) if job.upload else None
            if r2 is not None:
                r2.check()  # fail before spending minutes encoding
            source = probe(ffprobe, job.path)
        except (ToolMissingError, ProbeError, R2Error) as e:
            return self._finish(job, "failed", "Failed", error=str(e))
        if job.cancel.is_set():
            return self._finish(job, "cancelled", "Cancelled")

        rungs = pick_rungs(source.width, source.height)
        video_id = uuid.uuid4().hex
        out_dir = s.work_dir / video_id
        self._set(
            job,
            size=f"{source.width}x{source.height}",
            duration=source.duration,
            qualities=[r.label for r in rungs],
            gpu=job.use_gpu and nvenc_available(ffmpeg),
            video_id=video_id,
            out_dir=out_dir,
            message="Encoding",
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / INCOMPLETE).touch()
        started = time.monotonic()

        def on_encode(fraction: float) -> None:
            elapsed = time.monotonic() - started
            eta = elapsed / fraction - elapsed if 0.02 < fraction < 1 else None
            self._set(job, progress=fraction, eta_seconds=eta)

        try:
            command = build_command(ffmpeg, source, rungs, gpu=bool(job.gpu))
            encode(
                command,
                out_dir,
                source.duration,
                on_encode,
                cancel=job.cancel,
                expected_variants=len(rungs),
            )
        except CancelledError:
            shutil.rmtree(out_dir, ignore_errors=True)
            return self._finish(job, "cancelled", "Cancelled")
        except EncodeError as e:
            shutil.rmtree(out_dir, ignore_errors=True)
            return self._finish(job, "failed", "Encoding failed", error=str(e))
        (out_dir / INCOMPLETE).unlink(missing_ok=True)

        if r2 is None:
            self._set(job, progress=1.0)
            return self._finish(job, "done", "Encoded (not uploaded)")

        self._set(job, status="uploading", message="Uploading", progress=0.0, eta_seconds=None)

        def on_upload(done: int, total: int) -> None:
            self._set(job, progress=done / total)

        self._mark_pending(video_id)
        try:
            url = r2.upload_video(out_dir, video_id, on_upload, cancel=job.cancel)
        except CancelledError:
            self._unmark_pending(video_id)
            return self._finish(job, "cancelled", "Cancelled (encoded files kept)")
        except R2Error as e:
            self._unmark_pending(video_id)
            return self._finish(job, "failed", "Upload failed (encoded files kept)", error=str(e))
        self._unmark_pending(video_id)
        shutil.rmtree(out_dir, ignore_errors=True)
        self._set(job, url=url, progress=1.0)
        self._finish(job, "done", "Ready")
