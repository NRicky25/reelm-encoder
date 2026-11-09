"""The point-and-click interface: a small web page served on this PC only.

    uv run reelm-encoder ui        (or double-click "Reelm Encoder.cmd")

Security: it listens on 127.0.0.1 only, accepts only Host headers for this
machine (blocks DNS rebinding), and every /api call - reads included - needs a
custom header that other websites can't send (blocks cross-site requests).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from reelm_encoder.config import Settings, get_settings
from reelm_encoder.jobs import JobQueue
from reelm_encoder.r2 import R2Error
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

ACTION_HEADER = "x-reelm-encoder"
DEFAULT_PORT = 8800
VIDEO_TYPES = "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.m2ts *.flv *.wmv *.mpg *.mpeg"
STATUS_MAX_AGE = 300  # seconds before the ffmpeg/GPU/R2 check is redone

PICKER_SCRIPT = f"""
import json, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
root.lift(); root.focus_force()
paths = filedialog.askopenfilenames(
    parent=root, title="Choose videos to encode",
    filetypes=[("Videos", "{VIDEO_TYPES}"), ("All files", "*.*")])
print(json.dumps(list(paths)))
"""


class PickerError(RuntimeError):
    pass


def pick_files() -> list[str]:
    """Open the normal Windows file picker (in its own process, so it never blocks the server)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", PICKER_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        raise PickerError(f"The file picker failed: {result.stderr.strip()[-300:]}")
    try:
        return [p for p in json.loads(result.stdout.strip() or "[]") if p]
    except ValueError as e:
        raise PickerError("The file picker returned something unexpected.") from e


def default_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def create_app(
    settings: Settings | None = None,
    queue: JobQueue | None = None,
    picker=pick_files,
    allowed_hosts: set[str] | None = None,
    recovery_notes: list[str] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    queue = queue or JobQueue(settings)
    hosts = allowed_hosts if allowed_hosts is not None else default_hosts(DEFAULT_PORT)
    app = FastAPI(title="Reelm Encoder", docs_url=None, redoc_url=None, openapi_url=None)

    # status: one immutable snapshot, swapped atomically; refreshes run one at a time
    status_box: dict[str, tuple[float, dict[str, Any]]] = {}
    status_lock = threading.Lock()
    picker_lock = threading.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"detail": "Not allowed"}, status_code=403)
        is_page = request.method == "GET" and request.url.path == "/"
        if not is_page and request.headers.get(ACTION_HEADER) != "1":
            return JSONResponse({"detail": "Not allowed"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return files("reelm_encoder").joinpath("ui.html").read_text(encoding="utf-8")

    def compute_status() -> dict[str, Any]:
        result: dict[str, Any] = {"workDir": str(settings.work_dir)}
        try:
            ffmpeg = find_tool("ffmpeg", settings.ffmpeg_dir)
            find_tool("ffprobe", settings.ffmpeg_dir)
            result |= {"ffmpeg": True, "gpu": nvenc_available(ffmpeg)}
        except ToolMissingError as e:
            result |= {"ffmpeg": False, "gpu": False, "ffmpegError": str(e)}
        r2: dict[str, Any] = {"configured": settings.r2_ready, "bucket": settings.r2_bucket}
        if settings.r2_ready:
            try:
                queue.r2_factory(settings).check()
                r2 |= {"reachable": True, "publicUrl": settings.r2_public_url}
            except R2Error as e:
                r2 |= {"reachable": False, "error": str(e)}
        else:
            r2["missing"] = settings.missing_r2()
        result["r2"] = r2
        result["notes"] = list(recovery_notes or [])
        return result

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        """Is ffmpeg / the GPU / R2 ready? Checked over the network, so cached."""
        cached = status_box.get("data")
        if cached and time.monotonic() - cached[0] < STATUS_MAX_AGE:
            return cached[1]
        return refresh_status()

    @app.post("/api/status/refresh")
    def refresh_status() -> dict[str, Any]:
        with status_lock:  # concurrent refreshes wait for one check instead of each running one
            cached = status_box.get("data")
            if cached and time.monotonic() - cached[0] < 2:
                return cached[1]
            data = compute_status()
            status_box["data"] = (time.monotonic(), data)
            return data

    @app.post("/api/pick")
    def pick() -> dict[str, list[str]]:
        if not picker_lock.acquire(blocking=False):
            raise HTTPException(409, "A file picker is already open (check the taskbar).")
        try:
            return {"paths": picker()}
        except PickerError as e:
            raise HTTPException(500, str(e)) from e
        finally:
            picker_lock.release()

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return queue.snapshot()

    @app.post("/api/jobs")
    def add_jobs(body: dict[str, Any]) -> list[dict[str, Any]]:
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise HTTPException(422, "Send 1 to 200 file paths.")
        if not all(isinstance(p, str) and p for p in paths):
            raise HTTPException(422, "Paths must be text.")
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            raise HTTPException(400, f"File not found: {missing[0]}")
        upload, gpu = bool(body.get("upload", True)), bool(body.get("gpu", True))
        return [j.to_dict() for j in queue.add(paths, upload=upload, use_gpu=gpu)]

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, bool]:
        return {"ok": queue.cancel(job_id)}

    @app.post("/api/jobs/{job_id}/remove")
    def remove(job_id: str) -> dict[str, bool]:
        return {"ok": queue.remove(job_id)}

    @app.post("/api/jobs/{job_id}/open-folder")
    def open_folder(job_id: str) -> dict[str, bool]:
        job = queue.get(job_id)
        if job is None or job.out_dir is None or not job.out_dir.is_dir():
            raise HTTPException(404, "No folder for this video")
        os.startfile(job.out_dir)  # type: ignore[attr-defined]  # Windows only
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/delete-from-r2")
    def delete_from_r2(job_id: str) -> dict[str, int]:
        """Delete the R2 files of a video uploaded in this session (only those)."""
        job = queue.get(job_id)
        if job is None or job.status != "done" or not job.url or not job.video_id:
            raise HTTPException(404, "No uploaded video for this entry")
        try:
            deleted = queue.r2_factory(settings).delete_video(job.video_id)
        except R2Error as e:
            raise HTTPException(400, str(e)) from e
        queue.remove(job_id)
        return {"deleted": deleted}

    app.state.queue = queue
    return app


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _is_reelm_encoder(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return b"Reelm Encoder" in r.read(4096)
    except OSError:
        return False


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if not _port_free(port):
        if _is_reelm_encoder(url):
            print(f"Reelm Encoder is already running: {url}")
            if open_browser:
                webbrowser.open(url)
        else:
            print(
                f"Port {port} is used by another program. Try: reelm-encoder ui --port {port + 1}"
            )
        return 1
    settings = get_settings()
    queue = JobQueue(settings)
    notes = queue.recover()  # clean up after a window closed mid-job
    for note in notes:
        print(note)
    app = create_app(settings, queue, allowed_hosts=default_hosts(port), recovery_notes=notes)
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Reelm Encoder is running at {url}")
    print("Keep this window open while encoding. Close it to stop the encoder.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
