"""Local fake backend: implements the few endpoints the add-on uses, in the shape of the PopNow OpenAPI spec, for unit tests and the in-Blender self-test.

No network, no cost. Behavior:
- the key must be sk-popnow-test..., otherwise 401;
- the full three-step upload (PUT to a local URL, commit returns popnow://f/fil_...);
- quote returns fixed credits per model; createTask with expectedCredits must match maxCredits, otherwise 40901;
- tasks succeed after two polls (a task whose prompt contains FAIL fails and settles at 0 credits on the next read);
- outputs: an image is an 8x8 PNG, a video is a few bytes of fake mp4.
Usage: python3 tests/fake_server.py [port] (default 8799); set the base URL to http://127.0.0.1:<port>/api/v1
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VALID_KEY_PREFIX = "sk-popnow-test"


def tiny_png(w: int = 8, h: int = 8) -> bytes:
    """Generate a small grey PNG (no PIL needed)"""
    raw = b"".join(b"\x00" + bytes([128, 128, 128]) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        import struct

        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    import struct

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.files: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.idem: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.balance = 500
        self.fail_quote_once = False


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, status: int, body: dict | bytes, ctype: str = "application/json", headers: dict | None = None) -> None:
            raw = json.dumps(body).encode() if isinstance(body, dict) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _ok(self, data: dict, status: int = 200) -> None:
            self._send(status, {"code": 200, "msg": "success", "data": data, "request_id": "req_fake"})

        def _err(self, status: int, code: int, msg: str, reason: str = "") -> None:
            body = {"code": code, "msg": msg, "request_id": "req_fake_err"}
            if reason:
                body["data"] = {"reason": reason}
            self._send(status, body)

        def _auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer " + VALID_KEY_PREFIX):
                self._err(401, 401, "Invalid API key")
                return False
            return True

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _base(self) -> str:
            return f"http://{self.headers.get('Host')}"

        # ---------------- routes ----------------
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            state.calls.append(("GET", u.path))
            if u.path.startswith("/blob/"):
                fid = u.path.rsplit("/", 1)[-1]
                kind = fid.split(".")[-1]
                if kind == "png":
                    return self._send(200, tiny_png(), "image/png")
                return self._send(200, b"\x00\x00\x00\x18ftypmp42fake-video", "video/mp4")
            if not self._auth():
                return
            if u.path == "/api/v1/chat/credit":
                return self._ok({"available": f"{state.balance / 100:.2f}", "held": "0.00", "total": f"{state.balance / 100:.2f}", "availableCredits": state.balance, "heldCredits": 0, "totalCredits": state.balance})
            if u.path == "/api/v1/jobs/recordInfo":
                tid = parse_qs(u.query).get("taskId", [""])[0]
                with state.lock:
                    t = state.tasks.get(tid)
                    if not t:
                        return self._err(404, 404, "Task not found")
                    t["polls"] += 1
                    rec = self._record(t)
                return self._ok(rec)
            return self._err(404, 404, "Not found")

        def do_PUT(self):  # noqa: N802
            u = urlparse(self.path)
            state.calls.append(("PUT", u.path))
            data = self._body()
            fid = u.path.rsplit("/", 1)[-1]
            f = state.files.get(fid)
            if not f or len(data) != f["bytes"] or self.headers.get("X-Fake-Upload") != "yes":
                return self._send(400, b"bad upload", "text/plain")
            f["uploaded"] = True
            self._send(200, b"", "text/plain")

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            state.calls.append(("POST", u.path))
            raw = self._body()
            if not self._auth():
                return
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._err(400, 400, "Bad JSON")
            if u.path == "/api/v1/common/upload-url":
                fid = "fil_" + uuid.uuid4().hex[:12]
                state.files[fid] = {"bytes": body["bytes"], "contentType": body["contentType"], "uploaded": False}
                return self._ok({"fileId": fid, "key": f"popnow://f/{fid}", "uploadUrl": f"{self._base()}/put/{fid}", "method": "PUT", "headers": {"Content-Type": body["contentType"], "X-Fake-Upload": "yes"}, "expiresAt": "2099-01-01T00:00:00Z", "maxBytes": body["bytes"]})
            if u.path.startswith("/api/v1/files/") and u.path.endswith("/commit"):
                fid = u.path.split("/")[4]
                f = state.files.get(fid)
                if not f or not f["uploaded"]:
                    return self._err(409, 40003, "Upload incomplete")
                out = {"fileId": fid, "status": "ready", "bytes": f["bytes"], "contentType": f["contentType"], "sha256": "0" * 64, "uri": f"popnow://f/{fid}", "expiresAt": "2099-01-01T00:00:00Z"}
                if f["contentType"].startswith("video/"):
                    out["durationSeconds"] = "5.0"
                return self._ok(out)
            if u.path == "/api/v1/jobs/quote":
                if state.fail_quote_once:
                    state.fail_quote_once = False
                    return self._err(503, 503, "Busy", "")
                credits = self._price(body)
                return self._ok({"quoteId": f"q_{credits}_{uuid.uuid4().hex[:6]}", "model": body["model"], "estimatedCost": f"{credits / 100:.2f}", "maxCharge": f"{credits / 100:.2f}", "maxCredits": credits, "expectedCredits": credits, "currency": "USD", "quantity": "1", "unit": "image", "expiresAt": "2099-01-01T00:00:00Z"})
            if u.path == "/api/v1/jobs/createTask":
                key = self.headers.get("Idempotency-Key", "")
                with state.lock:
                    if key and key in state.idem:
                        t = state.tasks[state.idem[key]]
                        return self._ok({"taskId": t["taskId"], "state": t["state"], "estimatedCost": "0.06", "estimatedCredits": t["credits"], "deadlineAt": "2099-01-01T00:00:00Z"}, 202)
                    credits = self._price(body)
                    if body.get("expectedCredits") is not None and int(body["expectedCredits"]) != credits:
                        return self._err(409, 40901, "Price changed")
                    if credits > state.balance:
                        return self._err(402, 40201, "Insufficient balance", "insufficient_credits")
                    for v in _media_values(body.get("input") or {}):
                        if v.startswith("popnow://f/") and v[len("popnow://f/"):] not in state.files:
                            return self._err(400, 400, "Unknown file")
                    tid = "tsk_" + uuid.uuid4().hex[:16]
                    video = "video" in body["model"] or "reference-to-video" in body["model"] or "character-animation" in body["model"]
                    fail = "FAIL" in str((body.get("input") or {}).get("prompt", ""))
                    t = {"taskId": tid, "model": body["model"], "state": "queued", "credits": credits, "polls": 0, "video": video, "fail": fail, "input": body.get("input")}
                    state.tasks[tid] = t
                    if key:
                        state.idem[key] = tid
                    state.balance -= credits
                return self._ok({"taskId": tid, "state": "queued", "estimatedCost": f"{credits / 100:.2f}", "estimatedCredits": credits, "deadlineAt": "2099-01-01T00:00:00Z"}, 202)
            if u.path == "/api/v1/common/download-url":
                t = state.tasks.get(body.get("taskId", ""))
                if not t:
                    return self._err(404, 404, "Not found")
                ext = "mp4" if t["video"] else "png"
                return self._ok({"key": "out-1", "url": f"{self._base()}/blob/{t['taskId']}.{ext}", "expiresAt": "2099-01-01T00:00:00Z"})
            return self._err(404, 404, "Not found")

        def _price(self, body: dict) -> int:
            m = body.get("model", "")
            if "reference-to-video" in m or "video-edit" in m or "character-animation" in m:
                return 17
            if "turbo" in m:
                return 3
            return 6

        def _record(self, t: dict) -> dict:
            if t["polls"] >= 2 and t["state"] in ("queued", "running"):
                t["state"] = "failed" if t["fail"] else "succeeded"
            elif t["polls"] == 1:
                t["state"] = "running"
            rec = {"taskId": t["taskId"], "model": t["model"], "state": t["state"], "cost": f"{t['credits'] / 100:.2f}", "credits": t["credits"], "settled": False, "createdAt": "2026-10-16T00:00:00Z", "deadlineAt": "2099-01-01T00:00:00Z"}
            if t["state"] == "succeeded":
                rec["settled"] = True
                ext, mime = ("mp4", "video/mp4") if t["video"] else ("png", "image/png")
                rec["output"] = {"assets": [{"key": "out-1", "url": f"{self._base()}/blob/{t['taskId']}.{ext}", "mime": mime, "width": 8, "height": 8, "role": "video" if t["video"] else "image"}]}
            if t["state"] == "failed":
                rec["errorCode"] = "content_rejected"
                rec["errorMessage"] = "The model declined this request."
                if t["polls"] >= 3:
                    rec["settled"] = True
                    rec["credits"] = 0
                    rec["cost"] = "0.00"
                    if not t.get("refunded"):
                        t["refunded"] = True
                        state.balance += t["credits"]
            return rec

    return H


def _media_values(inp: dict):
    for v in inp.values():
        if isinstance(v, str):
            yield v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    yield x


def start(port: int = 0) -> tuple[ThreadingHTTPServer, State]:
    state = State()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    srv, _ = start(port)
    print(f"fake PopNow API on http://127.0.0.1:{srv.server_address[1]}/api/v1 (key: {VALID_KEY_PREFIX}…)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
