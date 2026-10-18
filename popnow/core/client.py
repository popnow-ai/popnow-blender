"""Minimal client for the PopNow API (standard library only, no bpy; runs and is tested in any Python 3.11+).

Contract (see https://popnow.ai/docs and https://popnow.ai/docs/openapi):
- Envelope {code, msg, data, request_id}; only code == 200 is success; branch on code on failure, never parse msg;
- Auth: Authorization: Bearer sk-popnow-...;
- Upload in three steps: common/upload-url -> PUT to the signed URL (copy the returned headers) -> files/{fileId}/commit, which yields popnow://f/...;
- Tasks: jobs/quote -> jobs/createTask (with quoteId + expectedCredits + Idempotency-Key) -> poll jobs/recordInfo;
- Output URLs are short-lived signed URLs; downloads do **not** send the key.
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.popnow.ai/api/v1"
CONSOLE_URL = "https://popnow.ai/api/console"
PRICING_URL = "https://popnow.ai/pricing"

# Media types the upload endpoint accepts (a subset of UploadURLRequest.contentType)
UPLOAD_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}
IMAGE_MAX_BYTES = 10 * 1024 * 1024
MEDIA_MAX_BYTES = 90 * 1024 * 1024


@dataclass
class ApiError(Exception):
    """Error returned by the API: http is the HTTP status, code is the business code (equals http when there is no envelope), reason is data.reason (may be empty)"""

    http: int
    code: int
    message: str
    request_id: str = ""
    reason: str = ""
    retry_after: float | None = None

    def __str__(self) -> str:  # one English sentence for logs and the UI
        return friendly_message(self)


@dataclass
class NetworkError(Exception):
    """Could not connect, timed out, certificate error and the like (no HTTP response was received)"""

    message: str

    def __str__(self) -> str:
        return f"Could not reach PopNow: {self.message}"


def friendly_message(err: ApiError) -> str:
    """English UI hint by business code (does not depend on the server's msg wording, but includes msg for codes without a specific hint)"""
    c = err.code
    if c == 401 or err.http == 401:
        return "Your API key was not accepted. Paste a valid key (sk-popnow-…) from the PopNow developer console."
    if c in (402, 40201, 40202) or err.reason == "insufficient_credits":
        return "Not enough credits. Top up at popnow.ai/pricing and try again."
    if c == 40310:
        return "Verify your email address first (check the link we emailed you), then try again."
    if c == 40301:
        return "This API key is not allowed to use this model. Check the key's model list in the developer console."
    if c == 40302:
        return "This API key has an IP allow-list that does not include this computer."
    if c == 40303:
        return "PopNow is not available in your region."
    if c == 40901:
        return "The price changed since the quote. Get a new quote and try again."
    if c == 40004:
        return f"This model cannot run with these settings. {err.message}".strip()
    if c == 413 or err.http == 413:
        return "The file is too large to upload."
    if c == 429 or err.http == 429:
        return "Too many requests right now. Wait a moment and try again."
    if c in (503, 50301, 50302) or err.http >= 500:
        return "The model is busy or unavailable right now. Try again in a minute or pick another model."
    if c == 404 or err.http == 404:
        return "Not found. The task may belong to another API key."
    msg = err.message or "Request failed"
    return f"{msg} (code {c})"


def _ssl_context() -> ssl.SSLContext:
    """Prefer certifi's root certificates (the Python bundled with Blender cannot read the system store on some macOS setups)"""
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def content_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in UPLOAD_TYPES:
        return UPLOAD_TYPES[ext]
    guess = mimetypes.guess_type(path)[0]
    if guess in UPLOAD_TYPES.values():
        return guess  # type: ignore[return-value]
    raise ValueError(f"Unsupported file type: {os.path.basename(path)}")


def mask_key(key: str) -> str:
    """The UI shows only the prefix and the last characters, never the full key"""
    key = (key or "").strip()
    if len(key) < 14:
        return "•" * len(key)
    return f"{key[:10]}…{key[-4:]}"


def key_looks_valid(key: str) -> bool:
    key = (key or "").strip()
    return key.startswith("sk-") and len(key) >= 20 and " " not in key


@dataclass
class Transport:
    """The layer that actually sends requests; replaced by a fake in unit tests. Returns (status, headers, body)"""

    timeout: float = 60.0
    context: ssl.SSLContext = field(default_factory=_ssl_context)

    def _open(self, req: urllib.request.Request):
        """Local addresses (a local backend during development) bypass the system proxy: with a proxy running, 127.0.0.1 would be forwarded to it and return 502"""
        host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.context))
            return opener.open(req, timeout=self.timeout)
        return urllib.request.urlopen(req, timeout=self.timeout, context=self.context)

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._open(req) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
            reason = getattr(e, "reason", e)
            raise NetworkError(str(reason)) from e

    def download(self, url: str, dest: str, on_progress: Callable[[int, int], None] | None = None, should_stop: Callable[[], bool] | None = None) -> int:
        """Stream a signed output URL to dest (write .part first, rename when complete); sends no auth header"""
        req = urllib.request.Request(url, headers={"User-Agent": "PopNow-Blender"})
        tmp = dest + ".part"
        try:
            with self._open(req) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if should_stop and should_stop():
                        raise InterruptedError("download stopped")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
            os.replace(tmp, dest)
            return done
        except urllib.error.HTTPError as e:
            _silent_remove(tmp)
            raise ApiError(e.code, e.code, "Download failed") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
            _silent_remove(tmp)
            raise NetworkError(str(getattr(e, "reason", e))) from e
        except BaseException:
            _silent_remove(tmp)
            raise


def _silent_remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


class PopNowClient:
    """One instance per API key; thread-safe (holds no mutable state)"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, user_agent: str = "PopNow-Blender", transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = user_agent
        self.transport = transport or Transport()
        self._sleep = sleep

    # ---------- low level ----------
    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        return url

    def request(self, method: str, path: str, body: dict | None = None, query: dict[str, Any] | None = None, headers: dict[str, str] | None = None, retries: int = 2, idempotent: bool = False) -> Any:
        """Send an enveloped request and return data. GETs, side-effect-free POSTs (idempotent=True, such as quotes) and writes with an idempotency key
        are retried with Retry-After backoff on network errors, 429 and 503"""
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Accept-Language": "en",
            "User-Agent": self.user_agent,
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        safe_to_retry = method == "GET" or idempotent or "Idempotency-Key" in h
        attempt = 0
        while True:
            try:
                status, rh, raw = self.transport.send(method, self._url(path, query), h, payload)
            except NetworkError:
                if safe_to_retry and attempt < retries:
                    attempt += 1
                    self._sleep(min(2.0 * attempt, 6.0))
                    continue
                raise
            err = _parse_error(status, rh, raw)
            if err is None:
                env = json.loads(raw.decode("utf-8")) if raw else {}
                return env.get("data") if isinstance(env, dict) else None
            if safe_to_retry and attempt < retries and (err.http in (429, 503) or err.code in (429, 503)):
                attempt += 1
                self._sleep(min(err.retry_after or 2.0 * attempt, 15.0))
                continue
            raise err

    # ---------- account ----------
    def balance(self) -> dict:
        """Balance: availableCredits / heldCredits / totalCredits (whole credits)"""
        return self.request("GET", "/chat/credit") or {}

    # ---------- tasks ----------
    def quote(self, model: str, input: dict) -> dict:
        return self.request("POST", "/jobs/quote", {"model": model, "input": input}, retries=2, idempotent=True)

    def create_task(self, model: str, input: dict, *, quote_id: str | None = None, expected_credits: int | None = None, idempotency_key: str | None = None) -> dict:
        body: dict[str, Any] = {"model": model, "input": input}
        if quote_id:
            body["quoteId"] = quote_id
        if expected_credits is not None:
            body["expectedCredits"] = int(expected_credits)
        key = idempotency_key or f"blender-{uuid.uuid4()}"
        # With an idempotency key, retrying with the same key after a network error is safe (same key and body returns the original task, nothing is held twice)
        return self.request("POST", "/jobs/createTask", body, headers={"Idempotency-Key": key}, retries=3)

    def record(self, task_id: str) -> dict:
        return self.request("GET", "/jobs/recordInfo", query={"taskId": task_id}, retries=3)

    def list_jobs(self, limit: int = 20) -> dict:
        return self.request("GET", "/jobs", query={"limit": limit})

    def download_url(self, task_id: str, key: str = "") -> dict:
        return self.request("POST", "/common/download-url", {"taskId": task_id, "key": key}, idempotent=True)

    # ---------- uploads ----------
    def upload_bytes(self, data: bytes, content_type: str) -> dict:
        """Three-step upload; returns the commit result (uri is popnow://f/...)"""
        limit = IMAGE_MAX_BYTES if content_type.startswith("image/") else MEDIA_MAX_BYTES
        if len(data) > limit:
            raise ApiError(413, 413, "File too large")
        ticket = self.request("POST", "/common/upload-url", {"contentType": content_type, "bytes": len(data)}, retries=1)
        put_headers = {str(k): str(v) for k, v in (ticket.get("headers") or {}).items()}
        put_headers.setdefault("Content-Type", content_type)
        status, rh, raw = self.transport.send(ticket.get("method") or "PUT", ticket["uploadUrl"], put_headers, data)
        if status >= 300:
            raise ApiError(status, status, "Upload failed")
        file_id = ticket["fileId"]
        return self.request("POST", f"/files/{urllib.parse.quote(file_id)}/commit", {}, headers={"Idempotency-Key": f"blender-commit-{file_id}"})

    def upload_file(self, path: str) -> dict:
        ctype = content_type_for(path)
        with open(path, "rb") as f:
            return self.upload_bytes(f.read(), ctype)

    def download(self, url: str, dest: str, on_progress: Callable[[int, int], None] | None = None, should_stop: Callable[[], bool] | None = None) -> int:
        return self.transport.download(url, dest, on_progress, should_stop)


def _parse_error(status: int, headers: dict[str, str], raw: bytes) -> ApiError | None:
    env: Any = None
    if raw:
        try:
            env = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            env = None
    retry_after = None
    ra = headers.get("retry-after")
    if ra:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    if isinstance(env, dict) and "code" in env:
        code = int(env.get("code") or status)
        if 200 <= status < 300 and code == 200:
            return None
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        return ApiError(status, code, str(env.get("msg") or ""), str(env.get("request_id") or ""), str((data or {}).get("reason") or ""), retry_after)
    if 200 <= status < 300:
        return None
    return ApiError(status, status, f"HTTP {status}", "", "", retry_after)
