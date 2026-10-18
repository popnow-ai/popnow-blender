"""The full flow of one generation (runs in a background thread, never touches bpy): upload inputs -> quote -> wait for confirmation -> create task -> poll -> download outputs.

The thread only changes the Job's own fields and tells the main thread "something changed" via notify(job); the main thread (bpy.app.timers) redraws panels
and loads downloaded files into Blender. There are two kinds of cancel:
- Before the task is created (uploading, quoting, awaiting confirmation): stop right away, nothing is charged;
- After the task was handed to PopNow: the API has no cancel endpoint, so this only means "stop waiting"; the task finishes and is billed on the server as usual,
    and its result can be fetched later with Resume in the task list. The UI must say so clearly.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .client import ApiError, NetworkError, PopNowClient, friendly_message

# Task states
ACTIVE = ("uploading", "quoting", "awaiting_confirm", "submitting", "queued", "running", "downloading")
TERMINAL = ("succeeded", "failed", "canceled")
# stopped: no longer waiting locally (user pressed stop, network dropped, Blender closed); the server task may still be running and can be resumed

EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}


@dataclass
class MediaFile:
    path: str
    slot: str  # structure / control / refs / clip / character
    seconds: float | None = None  # video length (for pricing)


@dataclass
class Job:
    workflow: str
    model: str
    label: str
    prompt: str
    user_prompt: str
    options: dict[str, Any]
    media: list[MediaFile]
    out_dir: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at: float = field(default_factory=time.time)
    state: str = "uploading"
    status: str = "Preparing…"
    progress: float = 0.0
    quote_id: str = ""
    max_credits: int | None = None
    expected_credits: int | None = None
    estimate: int | None = None
    task_id: str = ""
    credits: int | None = None
    settled: bool = False
    refunded: bool | None = None
    error: str = ""
    error_code: str = ""
    request_id: str = ""
    deadline_at: str = ""
    outputs: list[str] = field(default_factory=list)
    output_meta: list[dict] = field(default_factory=list)
    finished_at: float | None = None
    # Extra info for the main thread (target object, texture tiling, VSE position...); the thread never reads it
    extra: dict[str, Any] = field(default_factory=dict)
    # whether the main thread has already loaded the result into Blender
    applied: bool = False
    # URIs of inputs already uploaded (not uploaded again on Resume or retry)
    uploaded: dict[str, list[str]] = field(default_factory=dict)
    input: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE

    @property
    def resumable(self) -> bool:
        return self.state == "stopped" and bool(self.task_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["media"] = [asdict(m) for m in self.media]
        # History does not store the request body (it holds the prompt and input URIs; no need for a second copy on disk)
        d.pop("input", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["media"] = [MediaFile(**m) for m in d.get("media") or []]
        known = set(cls.__dataclass_fields__)
        job = cls(**{k: v for k, v in d.items() if k in known})
        # Unfinished when Blender last closed: the server task may be done already, so mark it resumable
        if job.state in ACTIVE:
            if job.task_id:
                job.state, job.status = "stopped", "Blender was closed while waiting. Resume to fetch the result."
            else:
                job.state, job.status = "canceled", "Stopped before it was submitted. Nothing was charged."
        return job


def refund_note(job: Job) -> str:
    if not job.settled:
        return "The held credits will be refunded."
    if job.credits == 0:
        return "Your credits were refunded."
    return f"{job.credits} credits were charged (this model bills refusals)."


class JobRunner(threading.Thread):
    """One thread per task. notify is called from the background thread; implementations may only do thread-safe things (such as putting into a queue)"""

    def __init__(
        self,
        client: PopNowClient,
        job: Job,
        notify: Callable[[Job], None],
        *,
        build_input: Callable[[Job, dict[str, list[str]]], dict] | None = None,
        auto_confirm_below: int = 0,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_limit_s: float = 3 * 3600,
    ):
        super().__init__(daemon=True, name=f"popnow-{job.id}")
        self.client = client
        self.job = job
        self.notify = notify
        self.build_input = build_input
        self.auto_confirm_below = auto_confirm_below
        self._confirm = threading.Event()
        self._cancel = threading.Event()
        self._sleep_fn = sleep
        self._clock = clock
        self.poll_limit_s = poll_limit_s

    # ---------- called from the main thread ----------
    def confirm(self) -> None:
        self._confirm.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._confirm.set()

    # ---------- helpers ----------
    def _set(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.job, k, v)
        self.notify(self.job)

    def _wait(self, seconds: float) -> bool:
        """Wait that can be interrupted by cancel; returns True when canceled"""
        if self._sleep_fn is not None:
            if self._cancel.is_set():
                return True
            self._sleep_fn(seconds)
            return self._cancel.is_set()
        return self._cancel.wait(seconds)

    # ---------- thread body ----------
    def run(self) -> None:
        job = self.job
        try:
            if job.task_id:
                self._poll_and_fetch()
                return
            self._upload()
            if self._stop_before_submit():
                return
            self._quote()
            if self._stop_before_submit():
                return
            if not self._await_confirm():
                return
            self._submit()
            self._poll_and_fetch()
        except ApiError as e:
            self._fail(friendly_message(e), e)
        except NetworkError as e:
            if job.task_id:
                self._set(state="stopped", status=f"{e}. The task is still on PopNow; resume to fetch it.")
            else:
                self._fail(str(e) + " Nothing was charged.")
        except InterruptedError:
            self._set(state="stopped", status="Stopped waiting. The task keeps running on PopNow; resume to fetch it.")
        except Exception as e:  # safety net: never let the thread die silently
            if job.task_id:
                self._set(state="stopped", status=f"Unexpected error ({e.__class__.__name__}). Resume to try again.")
            else:
                self._fail(f"Unexpected error: {e}")

    def _fail(self, message: str, err: ApiError | None = None) -> None:
        extra = {}
        if err is not None:
            extra["request_id"] = err.request_id
        if not self.job.task_id:
            message = f"{message} Nothing was charged." if "Nothing was charged" not in message else message
        self._set(state="failed", status=message, error=message, finished_at=time.time(), **extra)

    def _stop_before_submit(self) -> bool:
        if self._cancel.is_set():
            self._set(state="canceled", status="Canceled. Nothing was charged.", finished_at=time.time())
            return True
        return False

    def _upload(self) -> None:
        job = self.job
        total = max(1, len(job.media))
        self._set(state="uploading", status="Uploading reference files…", progress=0.02)
        uploaded: dict[str, list[str]] = {}
        for i, m in enumerate(job.media):
            if self._cancel.is_set():
                return
            res = self.client.upload_file(m.path)
            uploaded.setdefault(m.slot, []).append(res["uri"])
            if m.seconds is None and res.get("durationSeconds"):
                try:
                    m.seconds = float(res["durationSeconds"])
                except (TypeError, ValueError):
                    pass
            self._set(progress=0.02 + 0.18 * (i + 1) / total, status=f"Uploaded {i + 1} of {total}")
        job.uploaded = uploaded

    def _quote(self) -> None:
        job = self.job
        if self.build_input is not None:
            job.input = self.build_input(job, job.uploaded)
        self._set(state="quoting", status="Getting a price…", progress=0.22)
        q = self.client.quote(job.model, job.input)
        self._set(quote_id=q.get("quoteId", ""), max_credits=_int(q.get("maxCredits")), expected_credits=_int(q.get("expectedCredits")))

    def _await_confirm(self) -> bool:
        job = self.job
        mc = job.max_credits or 0
        if self.auto_confirm_below > 0 and mc <= self.auto_confirm_below:
            return True
        self._set(state="awaiting_confirm", status=f"Ready: up to {mc} credits. Confirm to start.", progress=0.25)
        # A quote is valid for 5 minutes; no confirmation after 4.5 minutes counts as giving up (nothing charged), the user can click again
        deadline = self._clock() + 270
        while not self._confirm.is_set():
            if self._clock() > deadline:
                self._set(state="canceled", status="The quote expired before you confirmed. Nothing was charged.", finished_at=time.time())
                return False
            if self._wait(0.25) and self._cancel.is_set():
                break
        if self._cancel.is_set():
            self._set(state="canceled", status="Canceled. Nothing was charged.", finished_at=time.time())
            return False
        return True

    def _submit(self) -> None:
        job = self.job
        self._set(state="submitting", status="Starting…", progress=0.28)
        try:
            res = self.client.create_task(job.model, job.input, quote_id=job.quote_id or None, expected_credits=job.max_credits, idempotency_key=f"blender-{job.id}")
        except ApiError as e:
            if e.code == 40901:
                # Quote expired or price changed: quote again and ask the user to confirm again (never charge the new price automatically)
                self._quote()
                if not self._await_confirm():
                    return
                res = self.client.create_task(job.model, job.input, quote_id=job.quote_id or None, expected_credits=job.max_credits, idempotency_key=f"blender-{job.id}-r")
            else:
                raise
        self._set(task_id=res.get("taskId", ""), state="queued", status="Queued on PopNow", credits=_int(res.get("estimatedCredits")), deadline_at=res.get("deadlineAt", ""))

    def _poll_and_fetch(self) -> None:
        job = self.job
        start = self._clock()
        typical = float(job.extra.get("typical_seconds") or 60)
        interval = 2.0
        rec: dict = {}
        while True:
            rec = self.client.record(job.task_id)
            state = rec.get("state", "")
            job.credits = _int(rec.get("credits"))
            job.settled = bool(rec.get("settled"))
            elapsed = self._clock() - start
            if state in ("succeeded", "failed", "canceled", "expired"):
                break
            frac = min(0.9, 0.3 + 0.6 * (elapsed / max(typical, 1.0)))
            label = "Queued on PopNow" if state == "queued" else "Generating"
            self._set(state="running" if state == "running" else "queued", status=f"{label}… {int(elapsed)}s (usually ~{int(typical)}s)", progress=frac)
            if elapsed > self.poll_limit_s:
                self._set(state="stopped", status="Still running on PopNow after a long time. Resume later to fetch it.")
                return
            if self._wait(interval):
                raise InterruptedError()
            interval = 2.0 if elapsed < 20 else (4.0 if elapsed < 120 else 6.0)
        state = rec.get("state")
        if state != "succeeded":
            # Failed: wait for settlement (usually a few seconds) so we can tell the user whether credits were refunded
            tries = 0
            while not rec.get("settled") and tries < 5:
                if self._wait(2.0):
                    break
                tries += 1
                rec = self.client.record(job.task_id)
            job.credits = _int(rec.get("credits"))
            job.settled = bool(rec.get("settled"))
            job.refunded = job.settled and job.credits == 0
            reason = rec.get("errorMessage") or ("The task expired." if state == "expired" else "Generation failed.")
            msg = f"{reason} {refund_note(job)}"
            self._set(state="failed", status=msg, error=msg, error_code=rec.get("errorCode", ""), finished_at=time.time(), progress=1.0)
            return
        self._download(rec)

    def _download(self, rec: dict) -> None:
        job = self.job
        assets = [a for a in ((rec.get("output") or {}).get("assets") or []) if not a.get("unavailable") and not a.get("deleted")]
        # Video extension and similar models also return a segment; only the main output is needed here
        assets = [a for a in assets if a.get("role") != "segment"] or assets
        if not assets:
            self._set(state="failed", status="The task finished without a file. Contact support with the task ID.", error="no output", finished_at=time.time())
            return
        os.makedirs(job.out_dir, exist_ok=True)
        self._set(state="downloading", status="Downloading result…", progress=0.92)
        outputs, meta = [], []
        for i, a in enumerate(assets):
            url = a.get("url")
            if not url:
                url = self.client.download_url(job.task_id, a.get("key", "")).get("url")
            ext = EXT_BY_MIME.get(str(a.get("mime") or "").split(";")[0], "") or _ext_from_url(url) or (".mp4" if job.workflow == "animate" else ".png")
            name = f"popnow_{job.workflow}_{job.task_id[-10:] or job.id}_{i + 1}{ext}"
            dest = os.path.join(job.out_dir, name)
            try:
                self.client.download(url, dest, should_stop=self._cancel.is_set)
            except ApiError as e:
                if e.http in (401, 403, 404, 410):
                    # Signed URL expired: read the record again for a fresh URL
                    fresh = self.client.download_url(job.task_id, a.get("key", "")).get("url")
                    self.client.download(fresh, dest, should_stop=self._cancel.is_set)
                else:
                    raise
            outputs.append(dest)
            meta.append({k: a.get(k) for k in ("mime", "width", "height", "durationSeconds", "role") if a.get(k) is not None})
        job.credits = _int(rec.get("credits"))
        self._set(state="succeeded", status=f"Done · {job.credits if job.credits is not None else '?'} credits", outputs=outputs, output_meta=meta, progress=1.0, finished_at=time.time())


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ext_from_url(url: str | None) -> str:
    if not url:
        return ""
    path = url.split("?", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in EXT_BY_MIME.values() else ""
