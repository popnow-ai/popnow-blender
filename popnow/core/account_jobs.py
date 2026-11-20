"""One generation in account mode (signed in with PopNow, paid with credits), in a background thread that never touches bpy.

Same Job, same history and the same states as JobRunner (jobs.py), different calls:
- inputs go up through an upload window (account.upload_files) and come back as popnow:// links;
- the price comes from estimate_cost (free, starts nothing), in credits; the user confirms it, unless it is at most the
  "Start without asking up to (credits)" preference;
- the tool is then called with max_credits set to the confirmed price. If the server asks again (needs_confirmation, or the
  price went up), the new price is shown and confirmed again: max_credits is never raised on the user's behalf;
- needs_consent is passed on to the user; consent: true is sent only after they tick it themselves;
- get_job is polled until the creation is done, and files[].url is downloaded without the sign-in token.

Which tool each workflow uses (no model is sent, so the server picks its default for the job):
- Render  -> edit_image: image_url is the clay render, more_image_urls the structure pass and reference images, aspect_ratio;
- Animate -> edit_video: video_url is the clay clip, reference_image_urls the reference images;
             with a character image -> copy_motion: video_url is the clay clip, image_url the character;
- Texture -> create_image with aspect_ratio 1:1.

Credit amounts live in Job's credit fields (max_credits = confirmed price, credits = what the creation used,
estimate = local estimate); job.mode == "account" tells the UI to show them as credits.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from .account import AccountClient, AccountError, SignInRequired, ToolResult, credits_text, upload_files
from .client import ApiError, NetworkError, Transport
from .jobs import EXT_BY_MIME, Job, _ext_from_url
from .models import ModelSpec

# Stop asking again after this many price changes in a row
MAX_REQUOTES = 3
# A confirmation older than this is dropped (the price may have moved); nothing is charged
CONFIRM_WINDOW_S = 270

# Neutral specs for the prompt recipes: no model naming conventions (no tokens), fields of the account tools
NEUTRAL_RENDER = ModelSpec(
    {
        "model": "",
        "workflow": "render",
        "label": "Auto",
        "task": "image-to-image",
        "media": {"structure": {"field": "image_url", "array": False, "max": 1}, "refs": {"field": "more_image_urls", "array": True, "max": 3}},
        "tokens": {},
    }
)
NEUTRAL_EDIT_VIDEO = ModelSpec(
    {
        "model": "",
        "workflow": "animate",
        "label": "Auto",
        "task": "video-edit",
        "media": {"clip": {"field": "video_url", "array": False, "max": 1}, "refs": {"field": "reference_image_urls", "array": True, "max": 2}},
        "tokens": {},
        "clip": {"min": 3, "max": 10, "minSide": 720},
        "typicalSeconds": 180,
    }
)
NEUTRAL_COPY_MOTION = ModelSpec(
    {
        "model": "",
        "workflow": "animate",
        "label": "Auto",
        "task": "character-animation",
        "media": {"clip": {"field": "video_url", "array": False, "max": 1}, "character": {"field": "image_url", "array": False, "max": 1}},
        "tokens": {},
        "clip": {"min": 3, "max": 30, "minSide": 720},
        "typicalSeconds": 240,
    }
)
NEUTRAL_TEXTURE = ModelSpec({"model": "", "workflow": "texture", "label": "Auto", "task": "text-to-image", "media": {}, "tokens": {}, "typicalSeconds": 40})

# Bundled models closest to what the server picks by default, used only for the local "about N credits" estimate
ESTIMATE_MODELS = {
    "edit_image": "google/nano-banana-2.1/edit",
    "create_image": "google/nano-banana-2.1/text-to-image",
    "edit_video": "kling/o3/video-edit",
}


def neutral_spec(workflow: str, character: bool = False) -> ModelSpec:
    if workflow == "render":
        return NEUTRAL_RENDER
    if workflow == "animate":
        return NEUTRAL_COPY_MOTION if character else NEUTRAL_EDIT_VIDEO
    return NEUTRAL_TEXTURE


def tool_for(job: Job) -> str:
    if job.workflow == "render":
        return "edit_image"
    if job.workflow == "animate":
        return "copy_motion" if any(m.slot == "character" for m in job.media) else "edit_video"
    return "create_image"


def tool_call(job: Job, uploaded: dict[str, list[str]]) -> tuple[str, dict]:
    """The tool and its arguments (without max_credits / consent) for this job's uploaded inputs"""
    tool = tool_for(job)
    prompt = (job.prompt or "").strip()
    args: dict[str, Any] = {}
    if tool == "edit_image":
        structure = list(uploaded.get("structure") or [])
        if not structure:
            raise AccountError("The clay render did not upload.")
        args = {"prompt": prompt, "image_url": structure[0]}
        more = structure[1:] + list(uploaded.get("refs") or [])
        if more:
            args["more_image_urls"] = more
        if job.options.get("aspect_ratio"):
            args["aspect_ratio"] = job.options["aspect_ratio"]
    elif tool == "edit_video":
        clip = uploaded.get("clip") or []
        if not clip:
            raise AccountError("The clay clip did not upload.")
        args = {"video_url": clip[0], "prompt": prompt}
        if uploaded.get("refs"):
            args["reference_image_urls"] = list(uploaded["refs"])
    elif tool == "copy_motion":
        clip, character = uploaded.get("clip") or [], uploaded.get("character") or []
        if not clip or not character:
            raise AccountError("The clay clip or the character image did not upload.")
        args = {"video_url": clip[0], "image_url": character[0]}
        if prompt:
            args["prompt"] = prompt
    else:
        args = {"prompt": prompt, "aspect_ratio": "1:1"}
    return tool, args


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


class AccountJobRunner(threading.Thread):
    """One thread per task. notify runs on the background thread and may only do thread-safe things (such as a queue put)"""

    def __init__(
        self,
        client: AccountClient,
        job: Job,
        notify: Callable[[Job], None],
        *,
        auto_confirm_below_credits: int = 0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_limit_s: float = 3 * 3600,
    ):
        super().__init__(daemon=True, name=f"popnow-{job.id}")
        self.client = client
        self.job = job
        self.notify = notify
        self.auto_confirm_below_credits = max(0, int(auto_confirm_below_credits or 0))
        self.transport = transport or Transport()
        self._confirm = threading.Event()
        self._cancel = threading.Event()
        self._consent = False
        self._sleep_fn = sleep
        self._clock = clock
        self.poll_limit_s = poll_limit_s
        self.tool = ""
        self.args: dict = {}
        self._last: ToolResult | None = None

    # ---------- called from the main thread ----------
    def confirm(self, consent: bool = False) -> None:
        """consent is True only when the user ticked the consent box themselves"""
        if consent:
            self._consent = True
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
            self.tool, self.args = tool_call(job, job.uploaded)
            self._quote()
            if self._stop_before_submit():
                return
            if not self._await_confirm(allow_auto=True):
                return
            if not self._submit():
                return
            self._poll_and_fetch()
        except SignInRequired as e:
            self._lost(str(e))
        except AccountError as e:
            self._lost(str(e))
        except ApiError as e:  # downloads report HTTP errors this way
            self._lost(f"Download failed ({e.http}).")
        except NetworkError as e:
            if job.task_id:
                self._set(state="stopped", status=f"{e}. The creation is still on PopNow; resume to fetch it.")
            else:
                self._fail(str(e))
        except InterruptedError:
            if job.task_id:
                self._set(state="stopped", status="Stopped waiting. The creation keeps going on PopNow; resume to fetch it.")
            else:
                self._set(state="canceled", status="Canceled. Nothing was charged.", finished_at=time.time())
        except Exception as e:  # safety net: never let the thread die silently
            if job.task_id:
                self._set(state="stopped", status=f"Unexpected error ({e.__class__.__name__}). Resume to try again.")
            else:
                self._fail(f"Unexpected error: {e}")

    def _lost(self, message: str) -> None:
        if self.job.task_id:
            self._set(state="stopped", status=f"{message} The creation is still on PopNow; resume to fetch it.")
        else:
            self._fail(message)

    def _fail(self, message: str) -> None:
        if not self.job.task_id and "Nothing was charged" not in message:
            message = f"{message} Nothing was charged."
        self._set(state="failed", status=message, error=message, finished_at=time.time())

    def _stop_before_submit(self) -> bool:
        if self._cancel.is_set():
            self._set(state="canceled", status="Canceled. Nothing was charged.", finished_at=time.time())
            return True
        return False

    def _upload(self) -> None:
        job = self.job
        if not job.media:
            job.uploaded = {}
            return
        if job.uploaded:  # Run again / retry after a quote problem: the links are still good
            return
        total = len(job.media)
        self._set(state="uploading", status="Uploading reference files…", progress=0.02)

        def on_file(done: int, n: int) -> None:
            self._set(progress=0.02 + 0.18 * done / max(1, n), status=f"Uploaded {done} of {n}")

        links = upload_files(self.client, [m.path for m in job.media], note="Files from Blender", should_stop=self._cancel.is_set, on_file=on_file)
        uploaded: dict[str, list[str]] = {}
        for m, link in zip(job.media, links):
            uploaded.setdefault(m.slot, []).append(link)
        job.uploaded = uploaded
        self._set(progress=0.2, status=f"Uploaded {total} of {total}")

    def _quote(self) -> None:
        self._set(state="quoting", status="Getting a price…", progress=0.22)
        r = self.client.estimate(self.tool, self.args)
        if r.is_error or r.credits is None:
            raise AccountError(r.text or "PopNow could not price this.")
        self._set(max_credits=r.credits, expected_credits=r.credits, quote_id="")

    def _await_confirm(self, *, allow_auto: bool) -> bool:
        """Wait for Start. allow_auto: the first quote may start on its own under the threshold; a new, higher price never does"""
        job = self.job
        price = job.max_credits
        needs_consent = bool(job.extra.get("needs_consent"))
        if allow_auto and not needs_consent and price is not None and self.auto_confirm_below_credits > 0 and price <= self.auto_confirm_below_credits:
            return True
        self._confirm.clear()
        if needs_consent:
            status = f"Ready: {credits_text(price)}. Confirm you have permission for the people in your images, then start."
        else:
            status = f"Ready: {credits_text(price)}. Confirm to start." if price else "Ready. Confirm to start."
        self._set(state="awaiting_confirm", status=status, progress=0.25)
        deadline = self._clock() + CONFIRM_WINDOW_S
        while not self._confirm.is_set():
            if self._clock() > deadline:
                self._set(state="canceled", status="The price expired before you confirmed. Nothing was charged.", finished_at=time.time())
                return False
            if self._wait(0.25) and self._cancel.is_set():
                break
        if self._cancel.is_set():
            self._set(state="canceled", status="Canceled. Nothing was charged.", finished_at=time.time())
            return False
        if needs_consent and not self._consent:
            # Start without the box ticked: ask again, never send consent on the user's behalf
            return self._await_confirm(allow_auto=False)
        return True

    def _submit(self) -> bool:
        """Call the tool with the confirmed price; True once a creation started"""
        job = self.job
        for attempt in range(MAX_REQUOTES + 1):
            self._set(state="submitting", status="Starting…", progress=0.28)
            args = dict(self.args)
            if job.max_credits:
                args["max_credits"] = int(job.max_credits)
            if self._consent:
                args["consent"] = True
            try:
                r = self.client.call_tool(self.tool, args)
            except NetworkError as e:
                # The call may have reached PopNow; it cannot be retried safely (it could start a second one)
                msg = f"{e}. If it started, it shows up in your PopNow library and is refunded automatically if it fails."
                self._set(state="failed", status=msg, error=msg, finished_at=time.time())
                return False
            if r.is_error:
                raise AccountError(r.text or "PopNow could not start this.")
            data = r.structured
            job_id = data.get("job_id")
            if isinstance(job_id, str) and job_id and not r.needs_confirmation:
                credits = _int(data.get("credits"))
                self._set(task_id=job_id, state="queued", status="Started on PopNow", credits=credits if credits is not None else job.max_credits)
                self._last = r
                return True
            if attempt == MAX_REQUOTES:
                break
            if r.needs_consent and not self._consent:
                job.extra["needs_consent"] = True
                if r.credits:
                    job.max_credits = r.credits
            elif r.needs_confirmation:
                # Asked again: show the (possibly new) price and wait for the user; never raise max_credits ourselves
                job.max_credits = r.credits if r.credits is not None else job.max_credits
            else:
                # "Not started: the price is now N credits" comes back as text only: price it again and ask again
                self._quote()
            if not self._await_confirm(allow_auto=False):
                return False
        raise AccountError("The price kept changing. Try again in a minute.")

    def _poll_and_fetch(self) -> None:
        job = self.job
        start = self._clock()
        typical = float(job.extra.get("typical_seconds") or (180 if job.workflow == "animate" else 60))
        interval = 2.0
        last: ToolResult | None = self._last
        while True:
            if last is None:
                last = self.client.get_job(job.task_id, 0)
                if last.is_error and not last.structured:
                    raise AccountError(last.text or "PopNow could not find this creation.")
            data = last.structured
            status = str(data.get("status") or "")
            if data.get("credits") is not None:
                job.credits = _int(data.get("credits"))
            if status in ("succeeded", "failed", "blocked"):
                break
            elapsed = self._clock() - start
            frac = min(0.9, 0.3 + 0.6 * (elapsed / max(typical, 1.0)))
            label = "Queued on PopNow" if status == "queued" else "Generating"
            self._set(state="queued" if status == "queued" else "running", status=f"{label}… {int(elapsed)}s (usually ~{int(typical)}s)", progress=frac)
            if elapsed > self.poll_limit_s:
                self._set(state="stopped", status="Still running on PopNow after a long time. Resume later to fetch it.")
                return
            if self._wait(interval):
                raise InterruptedError()
            interval = 2.0 if elapsed < 20 else (4.0 if elapsed < 120 else 6.0)
            last = None
        if status != "succeeded":
            reason = last.text.split("\n", 1)[0] if last.text else "Generation failed."
            msg = f"{reason} The credits for it are refunded automatically."
            if "refunded" in reason:
                msg = reason
            job.refunded = True
            self._set(state="failed", status=msg, error=msg, error_code="content_rejected" if status == "blocked" else status, finished_at=time.time(), progress=1.0, settled=True)
            return
        self._download(last)

    def _download(self, result: ToolResult) -> None:
        job = self.job
        files = [f for f in (result.structured.get("files") or []) if isinstance(f, dict) and isinstance(f.get("url"), str) and f.get("url")]
        if not files:
            self._set(state="failed", status="The creation finished without a file. Find it in your PopNow library.", error="no output", finished_at=time.time())
            return
        os.makedirs(job.out_dir, exist_ok=True)
        self._set(state="downloading", status="Downloading result…", progress=0.92)
        outputs, meta = [], []
        for i, f in enumerate(files):
            mime = str(f.get("type") or "").split(";")[0]
            ext = EXT_BY_MIME.get(mime, "") or _ext_from_url(f["url"]) or (".mp4" if job.workflow == "animate" else ".png")
            dest = os.path.join(job.out_dir, f"popnow_{job.workflow}_{job.task_id[-10:] or job.id}_{i + 1}{ext}")
            try:
                # Signed download link: no sign-in token is sent
                self.transport.download(f["url"], dest, should_stop=self._cancel.is_set)
            except ApiError as e:
                if e.http not in (401, 403, 404, 410):
                    raise
                # The link expired: ask for the creation again for a fresh one
                fresh = self.client.get_job(job.task_id, 0).structured.get("files") or []
                url = fresh[i].get("url") if i < len(fresh) and isinstance(fresh[i], dict) else None
                if not isinstance(url, str) or not url:
                    raise
                self.transport.download(url, dest, should_stop=self._cancel.is_set)
            outputs.append(dest)
            meta.append({k: v for k, v in (("mime", mime), ("popnow_link", f.get("popnow_link"))) if v})
        done = f"Done · {credits_text(job.credits)}" if job.credits is not None else "Done"
        self._set(state="succeeded", status=done, outputs=outputs, output_meta=meta, progress=1.0, finished_at=time.time(), settled=True)
