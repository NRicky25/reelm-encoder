"""A queue of encode jobs, processed one at a time in a background thread.

Used by the desktop window; the command line runs the same steps directly.
Each video is encoded into its own new folder: <out_root>/<video-name>-<code>/.
"""

import re
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from reelm_encoder.config import Settings
from reelm_encoder.encode import CancelledError, EncodeError, build_command, encode
from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, probe
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

Status = Literal["queued", "encoding", "done", "failed", "cancelled"]
FINISHED: set[str] = {"done", "failed", "cancelled"}
INCOMPLETE = ".incomplete"  # marker file: this folder's encode never finished


def folder_slug(name: str) -> str:
    """A folder name that's safe inside a link: "Episode 1 [HD]" -> "episode-1-hd"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:60].rstrip("-") or "video"


def new_output_dir(root: Path, name: str) -> Path:
    """Create and return a NEW folder root/name (or "name (2)", "name (3)"…).

    Never returns a folder that already existed, so cleaning up a failed or
    cancelled encode can't touch anything the user had there before.
    """
    root.mkdir(parents=True, exist_ok=True)
    base = name.strip().rstrip(".") or "video"
    for n in range(1, 1000):
        candidate = root / (base if n == 1 else f"{base} ({n})")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"Too many folders named {base!r} in {root}")


def unique_folder_name(video: Path) -> str:
    """<slug>-<6 hex>: unique per encode. Hosted files are cached "forever", so a
    re-encode must never reuse an old folder name (= link) once uploaded."""
    return f"{folder_slug(video.stem)}-{uuid.uuid4().hex[:6]}"


def remove_unfinished(root: Path) -> list[str]:
    """Delete encodes in root that never finished (a crash or a closed window).

    Only folders holding our INCOMPLETE marker are touched. Returns their names.
    """
    removed: list[str] = []
    if root.is_dir():
        for marker in root.glob(f"*/{INCOMPLETE}"):
            shutil.rmtree(marker.parent, ignore_errors=True)
            removed.append(marker.parent.name)
    return removed


@dataclass
class Job:
    path: Path
    use_gpu: bool
    out_root: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = "queued"
    progress: float = 0.0  # 0..1
    message: str = "Waiting…"
    eta_seconds: float | None = None
    # filled in as the job runs
    size: str | None = None
    duration: float | None = None
    qualities: list[str] = field(default_factory=list)
    gpu: bool | None = None
    out_dir: Path | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.path.name,
            "path": str(self.path),
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "eta": None if self.eta_seconds is None else round(self.eta_seconds),
            "size": self.size,
            "duration": None if self.duration is None else round(self.duration),
            "qualities": self.qualities,
            "gpu": self.gpu,
            "folder": str(self.out_dir) if self.out_dir and self.out_dir.exists() else None,
            "error": self.error,
        }


class JobQueue:
    """Jobs run one at a time. Every status change happens under one lock, so a
    job cancelled while queued can never start, and the window always sees a
    consistent state."""

    def __init__(self, settings: Settings, *, start: bool = True):
        self.settings = settings
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, daemon=True, name="encoder")
            self._thread.start()

    # ── public API (called from the window) ──
    def add(self, paths: list[str], *, use_gpu: bool, out_root: Path | None = None) -> list[Job]:
        root = out_root or self.settings.work_dir
        new = [Job(Path(p), use_gpu=use_gpu, out_root=root) for p in paths]
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
        """Forget a finished job (its encoded files stay on disk)."""
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
        """Cancel everything and stop the worker (stops ffmpeg, removes partial files)."""
        self._stopping = True
        with self._lock:
            for job in self.jobs:
                job.cancel.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

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
            source = probe(ffprobe, job.path)
        except (ToolMissingError, ProbeError) as e:
            return self._finish(job, "failed", "Failed", error=str(e))
        if job.cancel.is_set():
            return self._finish(job, "cancelled", "Cancelled")

        rungs = pick_rungs(source.width, source.height)
        try:
            out_dir = new_output_dir(job.out_root, unique_folder_name(job.path))
        except OSError as e:
            return self._finish(job, "failed", "Failed", error=f"Can't write to that folder: {e}")
        (out_dir / INCOMPLETE).touch()
        self._set(
            job,
            size=f"{source.width}x{source.height}",
            duration=source.duration,
            qualities=[r.label for r in rungs],
            gpu=job.use_gpu and nvenc_available(ffmpeg),
            out_dir=out_dir,
            message="Encoding",
        )
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
        self._set(job, progress=1.0)
        self._finish(job, "done", "Saved")
