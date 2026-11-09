"""The point-and-click interface: a small web page served on this PC only.

    uv run reelm-encoder ui        (or double-click "Reelm Encoder.cmd")

Security: it listens on 127.0.0.1 only, accepts only Host headers for this
machine (blocks DNS-rebinding), and every action needs a custom header that
other websites can't send without permission (blocks cross-site requests).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from reelm_encoder.config import Settings, get_settings
from reelm_encoder.jobs import JobQueue
from reelm_encoder.r2 import R2Error
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

ACTION_HEADER = "x-reelm-encoder"
VIDEO_TYPES = "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.m2ts *.flv *.wmv *.mpg *.mpeg"

PICKER_SCRIPT = f"""
import json, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
paths = filedialog.askopenfilenames(
    parent=root, title="Choose videos to encode",
    filetypes=[("Videos", "{VIDEO_TYPES}"), ("All files", "*.*")])
print(json.dumps(list(paths)))
"""


def pick_files() -> list[str]:
    """Open the normal Windows file picker (in its own process, so it never blocks the server)."""
    result = subprocess.run(
        [sys.executable, "-c", PICKER_SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3600,
    )
    try:
        return [p for p in json.loads(result.stdout.strip() or "[]") if p]
    except ValueError:
        return []


class AddJobs(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    upload: bool = True
    gpu: bool = True


class DeleteVideo(BaseModel):
    video_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def create_app(
    settings: Settings | None = None,
    queue: JobQueue | None = None,
    picker=pick_files,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    queue = queue or JobQueue(settings)
    app = FastAPI(title="Reelm Encoder", docs_url=None, redoc_url=None, openapi_url=None)
    status_cache: dict[str, Any] = {}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse({"detail": "Not allowed"}, status_code=403)
        if request.method != "GET" and request.headers.get(ACTION_HEADER) != "1":
            return JSONResponse({"detail": "Not allowed"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return files("reelm_encoder").joinpath("ui.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status(refresh: bool = False) -> dict[str, Any]:
        """Is ffmpeg / the GPU / R2 ready? (R2 is checked over the network, so cached.)"""
        if refresh or "at" not in status_cache or time.monotonic() - status_cache["at"] > 300:
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
            status_cache.clear()
            status_cache.update(result, at=time.monotonic())
        return {k: v for k, v in status_cache.items() if k != "at"}

    @app.post("/api/pick")
    def pick() -> dict[str, list[str]]:
        return {"paths": picker()}

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return queue.list()

    @app.post("/api/jobs")
    def add_jobs(body: AddJobs) -> list[dict[str, Any]]:
        missing = [p for p in body.paths if not os.path.isfile(p)]
        if missing:
            raise HTTPException(400, f"File not found: {missing[0]}")
        return [j.to_dict() for j in queue.add(body.paths, upload=body.upload, use_gpu=body.gpu)]

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

    @app.post("/api/delete")
    def delete_video(body: DeleteVideo) -> dict[str, int]:
        try:
            return {"deleted": queue.r2_factory(settings).delete_video(body.video_id)}
        except R2Error as e:
            raise HTTPException(400, str(e)) from e

    app.state.queue = queue
    return app


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def serve(port: int = 8800, open_browser: bool = True) -> int:
    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if not _port_free(port):
        # most likely already running in another window: just show it
        print(f"Port {port} is in use - Reelm Encoder is probably already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return 1
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = create_app(allowed_hosts=hosts)
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Reelm Encoder is running at {url}")
    print("Keep this window open while encoding. Close it to stop the encoder.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
