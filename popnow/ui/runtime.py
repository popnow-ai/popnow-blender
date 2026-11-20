"""Runtime: task table, event queue between background threads and the main thread, timers, history on disk, balance cache,
and the PopNow account session (account mode: signed in, paid with credits; used when no API key is set).

Background threads only put "which task changed" into EVENTS; every bpy operation happens in bpy.app.timers callbacks (main thread),
so the Blender UI does not freeze during generation.
"""

import os
import queue
import tempfile
import threading
import time
import webbrowser
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable

import bpy

from ..core import account, history
from ..core.account_jobs import AccountJobRunner
from ..core.client import ApiError, NetworkError, PopNowClient, friendly_message, key_looks_valid
from ..core.jobs import ACTIVE, Job, JobRunner
from ..core.money import credits_to_usd, first_usd, usd_text

# Add-on package name (bl_ext.<repo>.popnow as an extension, popnow for a legacy install): the preferences bl_idname must equal it
ADDON_ID = __package__.rsplit(".", 1)[0]
VERSION = "0.4.0"

JOBS: dict[str, Job] = {}
RUNNERS: dict[str, "JobRunner | AccountJobRunner"] = {}
EVENTS: "queue.SimpleQueue[tuple[str, Any]]" = queue.SimpleQueue()
# Balance cache. API mode: {"mode": "api", "usd": exact USD string, "held_usd": exact USD string, "at", "error"};
# account mode: {"mode": "account", "credits": int, "held_credits": int, "at", "error"}
BALANCE: dict[str, Any] = {}
# Sign-in progress: {"state": "waiting" | "error", "url": the sign-in link while waiting, "error": str}; empty when idle
LOGIN: dict[str, Any] = {}
_login_stop = threading.Event()
_login_attempt = 0
_store: "account.SessionStore | None" = None
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


def user_agent() -> str:
    v = ".".join(str(x) for x in bpy.app.version[:2])
    return f"PopNow-Blender/{VERSION} (Blender {v})"


def client() -> PopNowClient:
    return PopNowClient(api_key(), base_url(), user_agent=user_agent())


# ---------------- account mode ----------------


def mcp_url() -> str:
    p = prefs()
    url = (getattr(p, "mcp_url", "") or "").strip() if p else ""
    return url or os.environ.get("POPNOW_MCP_URL", "").strip() or account.DEFAULT_MCP_URL


def account_path() -> str:
    """account.json in the add-on's own user folder (extension_path_user), never in userpref.blend or a .blend"""
    return os.path.join(user_dir(), "account.json")


def account_store() -> account.SessionStore:
    global _store
    path = account_path()
    if _store is None or _store.path != path:
        _store = account.SessionStore(path)
    return _store


def signed_in() -> bool:
    return account_store().get() is not None


def mode() -> str:
    """'api' when an API key is set (USD), 'account' when signed in without a key (credits), '' when neither"""
    if api_key():
        return "api"
    return "account" if signed_in() else ""


def account_client() -> account.AccountClient:
    return account.AccountClient(account_store(), http=account.Http(user_agent=user_agent()), client_version=VERSION)


def auto_confirm_credits() -> int:
    """The "Start without asking up to (credits)" preference for account mode; 0 always asks"""
    p = prefs()
    try:
        return max(0, int(getattr(p, "auto_confirm_credits", 0) or 0)) if p else 0
    except (TypeError, ValueError):
        return 0


def sign_in() -> None:
    """Start signing in on a background thread: the browser opens on popnow.ai and comes back to a loopback port"""
    global _login_stop, _login_attempt
    if LOGIN.get("state") == "waiting" or not online_allowed():
        return
    # A fresh stop flag and attempt number per sign-in, so a canceled attempt that finishes late changes nothing
    _login_stop = stop = threading.Event()
    _login_attempt += 1
    attempt = _login_attempt
    LOGIN.clear()
    LOGIN.update({"state": "waiting", "url": "", "attempt": attempt})
    store, url, http = account_store(), mcp_url(), account.Http(user_agent=user_agent())

    def opened(link: str) -> None:
        EVENTS.put(("open_url", {"attempt": attempt, "url": link}))

    def work() -> None:
        try:
            session = account.login(http, url, open_url=opened, should_stop=stop.is_set)
            if stop.is_set():
                return
            store.set(session)
            EVENTS.put(("login", {"attempt": attempt, "ok": True}))
        except account.AccountError as e:
            EVENTS.put(("login", {"attempt": attempt, "state": "error", "error": str(e)}))
        except NetworkError as e:
            EVENTS.put(("login", {"attempt": attempt, "state": "error", "error": str(e)}))
        except OSError as e:
            EVENTS.put(("login", {"attempt": attempt, "state": "error", "error": f"Could not save the sign-in: {e.strerror or e}"}))

    threading.Thread(target=work, daemon=True, name="popnow-sign-in").start()
    ensure_timer()


def cancel_sign_in() -> None:
    _login_stop.set()
    LOGIN.clear()


def sign_out() -> None:
    """Forget the session right away; tell PopNow to revoke the tokens in the background (best effort)"""
    session = account_store().clear()
    BALANCE.clear()
    LOGIN.clear()
    if session is None or not online_allowed():
        return
    http = account.Http(user_agent=user_agent())
    threading.Thread(target=lambda: account.revoke(http, session), daemon=True, name="popnow-sign-out").start()


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


def _runner(job: Job, build_input: Callable | None = None, *, fresh: bool = True):
    if job.mode == "account":
        return AccountJobRunner(account_client(), job, notify, auto_confirm_below_credits=auto_confirm_credits() if fresh else 0)
    if fresh:
        return JobRunner(client(), job, notify, build_input=build_input, auto_confirm_below_usd=auto_confirm_usd())
    return JobRunner(client(), job, notify)


def start(job: Job, build_input: Callable | None = None) -> None:
    JOBS[job.id] = job
    runner = _runner(job, build_input)
    RUNNERS[job.id] = runner
    runner.start()
    ensure_timer()
    redraw()


def resume(job: Job) -> None:
    job.state, job.status, job.error = "queued", "Checking on PopNow…", ""
    runner = _runner(job, fresh=False)
    RUNNERS[job.id] = runner
    runner.start()
    ensure_timer()


def confirm(job_id: str, consent: bool = False) -> None:
    r = RUNNERS.get(job_id)
    if r is None:
        return
    if isinstance(r, AccountJobRunner):
        r.confirm(consent=consent)
    else:
        r.confirm()


def cancel(job_id: str) -> None:
    r = RUNNERS.get(job_id)
    if r:
        r.cancel()


def auto_confirm_usd() -> Decimal:
    """The "Start without asking up to" preference in USD (whole cents). A threshold saved by 0.1.x in credits is moved
    over once (1 credit = $0.001, rounded down to the cent, so it never asks less often than before)."""
    p = prefs()
    if p is None:
        return Decimal(0)
    legacy = int(getattr(p, "auto_confirm_below", 0) or 0)
    if legacy > 0 and not float(getattr(p, "auto_confirm_usd", 0.0) or 0.0):
        try:
            p.auto_confirm_usd = min(5.0, (legacy // 10) / 100)
            p.auto_confirm_below = 0
        except (AttributeError, TypeError, ValueError):
            return (credits_to_usd(legacy) or Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    value = float(getattr(p, "auto_confirm_usd", 0.0) or 0.0)
    return Decimal(f"{value:.2f}") if value > 0 else Decimal(0)


def refresh_balance() -> None:
    """Fetch the balance in the background without blocking the UI (USD with an API key, credits when signed in)"""
    if not online_allowed():
        return
    m = mode()
    if m == "account":
        _refresh_credits()
        return
    if m != "api":
        return
    c = client()

    def work() -> None:
        try:
            b = c.balance()
            # availableUsd / heldUsd are exact; the credit fields are read only when an older server sends no USD
            available = first_usd(b.get("availableUsd"), b.get("available"))
            if available is None:
                available = credits_to_usd(b.get("availableCredits"))
            held = first_usd(b.get("heldUsd"), b.get("held"))
            if held is None:
                held = credits_to_usd(b.get("heldCredits"))
            EVENTS.put(("balance", {"mode": "api", "usd": usd_text(available), "held_usd": usd_text(held), "at": time.time(), "error": ""}))
        except ApiError as e:
            EVENTS.put(("balance", {"mode": "api", "error": friendly_message(e), "at": time.time()}))
        except NetworkError as e:
            EVENTS.put(("balance", {"mode": "api", "error": str(e), "at": time.time()}))

    BALANCE["loading"] = True
    threading.Thread(target=work, daemon=True, name="popnow-balance").start()
    ensure_timer()


def _refresh_credits() -> None:
    c = account_client()

    def work() -> None:
        try:
            b = c.balance()
            EVENTS.put(("balance", {"mode": "account", "credits": b["credits"], "held_credits": b.get("held", 0), "at": time.time(), "error": ""}))
        except (account.AccountError, NetworkError) as e:
            EVENTS.put(("balance", {"mode": "account", "error": str(e), "at": time.time()}))

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
        if kind == "open_url":
            if payload.get("attempt") == LOGIN.get("attempt") and LOGIN.get("state") == "waiting":
                LOGIN["url"] = payload["url"]
                _open_in_browser(payload["url"])
            continue
        if kind == "login":
            if payload.get("attempt") != LOGIN.get("attempt"):
                continue  # a canceled sign-in finishing late
            LOGIN.clear()
            if payload.get("state") == "error":
                LOGIN.update({"state": "error", "error": payload.get("error", "")})
            elif payload.get("ok"):
                BALANCE.clear()
                refresh_balance()
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
    busy = any(j.is_active for j in JOBS.values()) or BALANCE.get("loading") or LOGIN.get("state") == "waiting"
    return 0.2 if busy else 1.0


def _open_in_browser(url: str) -> None:
    try:
        bpy.ops.wm.url_open(url=url)
    except Exception:
        webbrowser.open(url)


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
    cancel_sign_in()
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
