"""Desktop window: encode videos into a folder you choose.

    uv run reelm-encoder app         (or double-click "Reelm Encoder.lnk")

Each video is encoded into <chosen folder>/<video-name>-<code>/. Nothing is
uploaded: you upload the folder yourself, then add the link in the Reelm admin.
"""

import base64
import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import darkdetect
import webview

from reelm_encoder.config import PROJECT_ROOT, Settings, get_settings
from reelm_encoder.jobs import JobQueue, remove_unfinished
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

HERE = Path(__file__).parent
PAGE = HERE / "desktop.html"
ICON_FILE = HERE / "assets" / "icon.ico"
PREFS_FILE = PROJECT_ROOT / "desktop-settings.json"
TITLE = "Reelm Encoder"
APP_ID = "reelm.encoder"
DEFAULT_DEST = Path.home() / "Videos" / "Reelm Encoded"
VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".m2ts", ".mts", ".flv",
    ".wmv", ".mpg", ".mpeg", ".3gp",
}  # fmt: skip
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

window: webview.Window | None = None  # kept out of Api so pywebview doesn't expose it


# ---------------- small Windows helpers ----------------
def load_prefs() -> dict[str, Any]:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("destination", str(DEFAULT_DEST))
    if data.get("device") not in ("gpu", "cpu"):
        data["device"] = "gpu"
    return data


def save_prefs(data: dict[str, Any]) -> None:
    try:
        PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def set_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(13, handle))  # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def gpu_name() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
        )  # fmt: skip
        return out.stdout.strip().splitlines()[0].replace("NVIDIA ", "").replace("GeForce ", "")
    except (OSError, IndexError, subprocess.SubprocessError):
        return ""


def thumbnail(ffmpeg: str, path: Path, at_seconds: float) -> str:
    """A small JPEG frame as a data URL ('' if it can't be made)."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-ss", f"{at_seconds:.1f}", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=320:-2",
             "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
            capture_output=True, timeout=20, creationflags=NO_WINDOW,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0 or not out.stdout:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(out.stdout).decode()


def already_running() -> bool:
    """True if another Reelm Encoder window is open (and bring it to the front)."""
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.CreateMutexW(None, False, "Local\\ReelmEncoderDesktop")
    if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return False
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return True


# ---------------- API the page can call ----------------
class Api:
    def __init__(self, settings: Settings, queue: JobQueue) -> None:
        self._settings = settings
        self._queue = queue
        self._prefs = load_prefs()
        self._thumbs: dict[str, str] = {}
        self._status: dict[str, Any] | None = None

    def _check(self) -> dict[str, Any]:
        if self._status is None:
            status: dict[str, Any] = {"publicUrl": self._settings.public_url or ""}
            try:
                ffmpeg = find_tool("ffmpeg", self._settings.ffmpeg_dir)
                find_tool("ffprobe", self._settings.ffmpeg_dir)
                gpu = nvenc_available(ffmpeg)
                status |= {"ffmpeg": True, "gpu": gpu, "gpuName": gpu_name() if gpu else ""}
            except ToolMissingError as e:
                status |= {"ffmpeg": False, "gpu": False, "ffmpegError": str(e)}
            self._status = status
        return self._status

    def init(self) -> dict[str, Any]:
        status = self._check()
        device = self._prefs["device"] if status["gpu"] else "cpu"
        return {"destination": self._prefs["destination"], "device": device, "status": status}

    def set_device(self, device: str) -> None:
        if device in ("gpu", "cpu"):
            self._prefs["device"] = device
            save_prefs(self._prefs)

    def choose_folder(self) -> str | None:
        assert window is not None
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        kind = kind or webview.FOLDER_DIALOG
        start = Path(self._prefs["destination"])
        start = next((p for p in (start, start.parent) if p.is_dir()), Path.home())
        picked = window.create_file_dialog(kind, directory=str(start))
        if not picked:
            return None
        folder = picked[0] if isinstance(picked, (list, tuple)) else picked
        self._prefs["destination"] = str(Path(folder))
        save_prefs(self._prefs)
        return self._prefs["destination"]

    def choose_files(self) -> list[str]:
        assert window is not None
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        start = Path(self._prefs.get("last_dir") or Path.home() / "Videos")
        exts = ";".join(f"*{e}" for e in sorted(VIDEO_EXT))
        picked = window.create_file_dialog(
            kind,
            directory=str(start if start.is_dir() else Path.home()),
            allow_multiple=True,
            file_types=(f"Videos ({exts})", "All files (*.*)"),
        )
        if not picked:
            return []
        paths = [str(p) for p in picked]
        self._prefs["last_dir"] = str(Path(paths[0]).parent)
        save_prefs(self._prefs)
        return paths

    def add(self, paths: list[str]) -> dict[str, Any]:
        good = [p for p in paths if Path(p).is_file() and Path(p).suffix.lower() in VIDEO_EXT]
        skipped = [p for p in paths if p not in good]
        if not good:
            return {"added": 0, "skipped": skipped}
        dest = Path(self._prefs["destination"]).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"error": f"Can't use the save folder: {e}"}
        use_gpu = self._prefs["device"] == "gpu" and self._check()["gpu"]
        jobs = self._queue.add(good, use_gpu=use_gpu, out_root=dest)
        for job in jobs:
            threading.Thread(target=self._make_thumb, args=(job.id, job.path), daemon=True).start()
        return {"added": len(jobs), "skipped": skipped}

    def _make_thumb(self, job_id: str, path: Path) -> None:
        try:
            ffmpeg = find_tool("ffmpeg", self._settings.ffmpeg_dir)
        except ToolMissingError:
            return
        # wait briefly for the queue to probe the length, so the frame isn't a black intro
        job = self._queue.get(job_id)
        for _ in range(40):
            if job is None or job.duration:
                break
            threading.Event().wait(0.25)
        at = (job.duration * 0.1) if job and job.duration and job.duration > 4 else 0.0
        self._thumbs[job_id] = thumbnail(ffmpeg, path, at)

    def jobs(self) -> list[dict[str, Any]]:
        out = []
        for j in self._queue.snapshot():
            j["thumb"] = self._thumbs.get(j["id"], "")
            out.append(j)
        return out

    def cancel(self, job_id: str) -> bool:
        return self._queue.cancel(job_id)

    def remove(self, job_id: str) -> bool:
        self._thumbs.pop(job_id, None)
        return self._queue.remove(job_id)

    def open_folder(self, path: str | None) -> None:
        folder = Path(path) if path else Path(self._prefs["destination"]).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # type: ignore[attr-defined]  # Windows only

    def copy(self, text: str) -> bool:
        return set_clipboard(str(text))


# ---------------- native window touches ----------------
def style_native_window() -> None:
    """Our icon in the title bar/taskbar, and a title bar matching the page."""
    user32 = ctypes.windll.user32
    try:
        hwnd = int(window.native.Handle.ToInt64())  # type: ignore[union-attr]
    except Exception:
        hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    if ICON_FILE.exists():
        user32.LoadImageW.restype = ctypes.c_void_p
        ptr = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ptr, ctypes.c_uint, ptr, ptr]
        for which, metric in ((1, 11), (0, 49)):  # ICON_BIG/SM_CXICON, ICON_SMALL/SM_CXSMICON
            px = user32.GetSystemMetrics(metric)
            icon = user32.LoadImageW(None, str(ICON_FILE), 1, px, px, 0x10)  # LR_LOADFROMFILE
            if icon:
                user32.SendMessageW(hwnd, 0x80, which, icon)  # WM_SETICON
    dark = (darkdetect.theme() or "Light") == "Dark"
    dwm = ctypes.windll.dwmapi
    on = ctypes.c_int(1 if dark else 0)
    dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # dark title bar
    caption = ctypes.c_int(0x00211D1C if dark else 0x00F9F5F6)  # page background, 0x00BBGGRR
    dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)


def hook_file_drop() -> None:
    """Files dropped on the window arrive here with their full paths."""
    try:
        from webview.dom import DOMEventHandler
    except ImportError:
        return

    def on_drop(e: dict[str, Any]) -> None:
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths and window is not None:
            window.evaluate_js(f"window.addDropped({json.dumps(paths)})")

    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)  # type: ignore[union-attr]


def run(on_ready: Any = None, settings: Settings | None = None) -> int:
    global window
    if on_ready is None and already_running():
        return 0
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass

    settings = settings or get_settings()
    # an encode cut off by a crash or power loss leaves a marked, half-written folder
    remove_unfinished(Path(load_prefs()["destination"]).expanduser())
    queue = JobQueue(settings)
    dark = (darkdetect.theme() or "Light") == "Dark"
    window = webview.create_window(
        TITLE,
        url=str(PAGE),
        js_api=Api(settings, queue),
        width=1040,
        height=800,
        min_size=(740, 620),
        background_color="#1C1D21" if dark else "#F6F5F9",
    )

    def on_closing() -> bool:
        if queue.busy() and window is not None:
            return bool(
                window.create_confirmation_dialog(
                    TITLE, "A video is still encoding. Stop it and close Reelm Encoder?"
                )
            )
        return True

    window.events.shown += lambda *_: style_native_window()
    window.events.loaded += lambda *_: hook_file_drop()
    window.events.closing += on_closing
    try:
        webview.start(on_ready, window) if on_ready else webview.start()
    finally:
        queue.stop()  # stops ffmpeg and removes a half-finished encode
    return 0


if __name__ == "__main__":
    sys.exit(run())
