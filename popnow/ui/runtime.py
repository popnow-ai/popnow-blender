"""Runtime: task table, event queue between background threads and the main thread, timers, history on disk, balance cache.

Background threads only put "which task changed" into EVENTS; every bpy operation happens in bpy.app.timers callbacks (main thread),
so the Blender UI does not freeze during generation.
"""

import os
import queue
import tempfile
import threading
import time
from typing import Any, Callable

import bpy

from ..core import history
from ..core.client import ApiError, NetworkError, PopNowClient, friendly_message, key_looks_valid
from ..core.jobs import ACTIVE, Job, JobRunner

# Add-on package name (bl_ext.<repo>.popnow as an extension, popnow for a legacy install): the preferences bl_idname must equal it
ADDON_ID = __package__.rsplit(".", 1)[0]
VERSION = "0.1.0"

JOBS: dict[str, Job] = {}
RUNNERS: dict[str, JobRunner] = {}
EVENTS: "queue.SimpleQueue[tuple[str, Any]]" = queue.SimpleQueue()
# Balance cache: {"credits": int, "held": int, "at": timestamp, "error": str}
BALANCE: dict[str, Any] = {}
_history_loaded = False
_apply_job: Callable[[Job], None] | None = None


def prefs():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def api_key() -> str:
    """The key from preferences; falls back to the POPNOW_API_KEY environment variable (handy for the command line and the self-test)"""
    p = prefs()
    key = (getattr(p, "api_key", "") or "").strip() if p else ""
    return key or os.environ.get("POPNOW_API_KEY", "").strip()


def base_url() -> str:
    p = prefs()
    url = (getattr(p, "base_url", "") or "").strip() if p else ""
    return url or os.environ.get("POPNOW_API_BASE", "").strip() or "https://api.popnow.ai/api/v1"


def online_allowed() -> bool:
    """Blender 4.2+ "Allow Online Access" switch; while it is off the extension must not send any request"""
    return bool(getattr(bpy.app, "online_access", True))


def client() -> PopNowClient:
    v = ".".join(str(x) for x in bpy.app.version[:2])
    return PopNowClient(api_key(), base_url(), user_agent=f"PopNow-Blender/{VERSION} (Blender {v})")


def user_dir(sub: str = "") -> str:
    """The add-on's own user directory (history, default output folder). Extensions use extension_path_user; legacy installs fall back to Blender's data directory"""
    try:
        base = bpy.utils.extension_path_user(ADDON_ID, create=True)
    except Exception:
        base = bpy.utils.user_resource("DATAFILES", path="popnow", create=True) or os.path.join(tempfile.gettempdir(), "popnow-blender")
    path = os.path.join(base, sub) if sub else base
    os.makedirs(path, exist_ok=True)
    return path


def output_dir() -> str:
    """Where results are saved: the preference when set; otherwise popnow/ next to the .blend when it is saved, or the add-on user directory when it is not"""
    p = prefs()
    custom = (getattr(p, "output_dir", "") or "").strip() if p else ""
    if custom:
        path = bpy.path.abspath(custom)
    elif bpy.data.filepath:
        path = os.path.join(os.path.dirname(bpy.data.filepath), "popnow")
    else:
        path = user_dir("outputs")
    os.makedirs(path, exist_ok=True)
    return path


def history_path() -> str:
    return os.path.join(user_dir(), "history.json")


def load_history() -> None:
    global _history_loaded
    if _history_loaded:
        return
    _history_loaded = True
    for job in history.load(history_path()):
        JOBS.setdefault(job.id, job)


def save_history() -> None:
    try:
        history.save(history_path(), list(JOBS.values()))
    except OSError as e:
        print(f"PopNow: could not save task history: {e}")


def jobs_sorted() -> list[Job]:
    return sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)


def notify(job: Job) -> None:
    """Called from background threads: only puts into the queue"""
    EVENTS.put(("job", job.id))


def start(job: Job, build_input: Callable | None = None) -> None:
    p = prefs()
    auto = int(getattr(p, "auto_confirm_below", 0) or 0) if p else 0
    JOBS[job.id] = job
    runner = JobRunner(client(), job, notify, build_input=build_input, auto_confirm_below=auto)
    RUNNERS[job.id] = runner
    runner.start()
    ensure_timer()
    redraw()


def resume(job: Job) -> None:
    job.state, job.status, job.error = "queued", "Checking on PopNow…", ""
    runner = JobRunner(client(), job, notify)
    RUNNERS[job.id] = runner
    runner.start()
    ensure_timer()


def confirm(job_id: str) -> None:
    r = RUNNERS.get(job_id)
    if r:
        r.confirm()


def cancel(job_id: str) -> None:
    r = RUNNERS.get(job_id)
    if r:
        r.cancel()


def refresh_balance() -> None:
    """Fetch the balance in the background without blocking the UI"""
    if not api_key() or not online_allowed():
        return
    c = client()

    def work() -> None:
        try:
            b = c.balance()
            EVENTS.put(("balance", {"credits": int(b.get("availableCredits") or 0), "held": int(b.get("heldCredits") or 0), "at": time.time(), "error": ""}))
        except ApiError as e:
            EVENTS.put(("balance", {"error": friendly_message(e), "at": time.time()}))
        except NetworkError as e:
            EVENTS.put(("balance", {"error": str(e), "at": time.time()}))

    BALANCE["loading"] = True
    threading.Thread(target=work, daemon=True, name="popnow-balance").start()
    ensure_timer()


def key_status() -> str:
    """'missing' / 'malformed' / 'ok'"""
    k = api_key()
    if not k:
        return "missing"
    return "ok" if key_looks_valid(k) else "malformed"


# ---------------- main thread timer ----------------


def set_apply_handler(fn: Callable[[Job], None]) -> None:
    global _apply_job
    _apply_job = fn


def _tick() -> float:
    changed = False
    finished = False
    while True:
        try:
            kind, payload = EVENTS.get_nowait()
        except queue.Empty:
            break
        changed = True
        if kind == "balance":
            BALANCE.clear()
            BALANCE.update(payload)
            continue
        job = JOBS.get(payload)
        if job is None:
            continue
        if job.state == "succeeded" and not job.applied and _apply_job is not None:
            job.applied = True
            try:
                _apply_job(job)
            except Exception as e:  # failing to load a result does not affect the task itself
                job.extra["apply_error"] = str(e)
                print(f"PopNow: could not load the result into Blender: {e}")
        if not job.is_active:
            RUNNERS.pop(job.id, None)
            finished = True
    if finished:
        save_history()
        refresh_balance()
    if changed:
        redraw()
    return 0.2 if any(j.is_active for j in JOBS.values()) or BALANCE.get("loading") else 1.0


def ensure_timer() -> None:
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.1, persistent=True)


def stop_timer() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)


def redraw() -> None:
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in ("VIEW_3D", "IMAGE_EDITOR", "SEQUENCE_EDITOR", "PREFERENCES"):
                area.tag_redraw()


def shutdown() -> None:
    """On unregister: stop all waiting (tasks already handed to PopNow keep running on the server and can be resumed next time), save to disk"""
    for r in list(RUNNERS.values()):
        r.cancel()
    for r in list(RUNNERS.values()):
        r.join(timeout=1.5)
    for job in JOBS.values():
        if job.is_active:
            if job.task_id:
                job.state, job.status = "stopped", "Stopped waiting. Resume to fetch the result."
            else:
                job.state, job.status = "canceled", "Canceled. Nothing was charged."
    save_history()
    stop_timer()
