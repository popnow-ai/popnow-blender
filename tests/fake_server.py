"""Local fake backend: implements the few endpoints the add-on uses, in the shape of the PopNow OpenAPI spec, for unit tests and the in-Blender self-test.

No network, no cost. Behavior:
- the key must be sk-popnow-test..., otherwise 401;
- the full three-step upload (PUT to a local URL, commit returns popnow://f/fil_...);
- money in the current API shape: exact USD strings (maxChargeUsd, estimatedCostUsd, costUsd, availableUsd) next to the
  deprecated credit fields (1 credit = $0.001); with state.legacy = True only the older fields are sent (USD aliases and credits);
- quote returns a fixed price per model; createTask with expectedCostUsd (or the deprecated expectedCredits) must match the
  quote's maximum, otherwise 40901;
- tasks succeed after two polls (a task whose prompt contains FAIL fails and settles at $0 on the next read);
- outputs: an image is an 8x8 PNG, a video is a few bytes of fake mp4.

Account mode (signed in with PopNow, credits) on the same port, in the shape of api.popnow.ai:
- OAuth discovery (/.well-known/oauth-protected-resource/mcp, /.well-known/oauth-authorization-server), dynamic registration,
  /oauth/authorize answering 302 back to the loopback redirect with code, state and iss, /oauth/token (authorization_code with
  PKCE S256 checked, refresh_token), /oauth/revoke;
- /mcp: Streamable HTTP JSON-RPC answering as JSON or as an event stream (state.sse), with check_balance, estimate_cost,
  request_upload, get_upload, edit_image, edit_video, copy_motion, create_image and get_job. A paid tool without enough
  max_credits answers needs_confirmation (and charges nothing); state.expire_token makes the next call with that token a 401;
- /console/v1/upload-sessions/<token>/files, /files/<id>/commit, /close (public: the token in the path is the credential).
Usage: python3 tests/fake_server.py [port] (default 8799); set the base URL to http://127.0.0.1:<port>/api/v1
and the account server URL to http://127.0.0.1:<port>/mcp
"""

from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal
import sys
import threading
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

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
        self.balance = 5000  # in credits internally; sent as USD (1 credit = $0.001)
        self.fail_quote_once = False
        # True: answer like a server from before the USD fields (only estimatedCost / maxCharge / cost / available and credits)
        self.legacy = False
        # The last createTask body, so tests can check what the add-on confirmed
        self.last_create: dict = {}
        # ---------- account mode ----------
        self.credits = 12000
        self.registered: list[dict] = []
        self.codes: dict[str, dict] = {}
        self.tokens = {"issued": 0, "refreshed": 0, "revoked": []}
        self.valid_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.expire_token = ""  # the next MCP call with this access token gets one 401
        self.sse = True  # answer tools/* as an event stream (initialize always answers as plain JSON)
        self.mcp_calls: list[dict] = []
        self.prices = {"edit_image": 48, "create_image": 48, "edit_video": 520, "copy_motion": 610}
        self.bump_once = 0  # add this to the price of the next paid call (the price went up after the quote)
        self.consent_tools: set[str] = set()  # tools that answer needs_consent until consent: true
        self.sessions: dict[str, dict] = {}
        self.account_jobs: dict[str, dict] = {}
        self.upload_auth: list[str] = []  # Authorization headers seen on upload-session and storage requests
        self.download_auth: list[str] = []
        self.bad_issuer = False
        self.estimate_offset = 0  # estimate_cost answers this much less than the tool really costs


def usd(credits: int) -> str:
    """Exact USD string for an amount in credits, the way the API writes it ("0.036")"""
    text = format(Decimal(credits) / 1000, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


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
                state.download_auth.append(self.headers.get("Authorization", ""))
                fid = u.path.rsplit("/", 1)[-1]
                kind = fid.split(".")[-1]
                if kind == "png":
                    return self._send(200, tiny_png(), "image/png")
                return self._send(200, b"\x00\x00\x00\x18ftypmp42fake-video", "video/mp4")
            if u.path.startswith(("/.well-known/", "/oauth/")):
                return self._oauth_get(u)
            if not self._auth():
                return
            if u.path == "/api/v1/chat/credit":
                out = {"available": usd(state.balance), "held": "0", "total": usd(state.balance), "availableCredits": state.balance, "heldCredits": 0, "totalCredits": state.balance}
                if not state.legacy:
                    out.update({"availableUsd": usd(state.balance), "heldUsd": "0", "totalUsd": usd(state.balance)})
                return self._ok(out)
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
            if f and f.get("session"):
                state.upload_auth.append(self.headers.get("Authorization", ""))
            if not f or len(data) != f["bytes"] or self.headers.get("X-Fake-Upload") != "yes":
                return self._send(400, b"bad upload", "text/plain")
            f["uploaded"] = True
            self._send(200, b"", "text/plain")

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            state.calls.append(("POST", u.path))
            raw = self._body()
            if u.path.startswith("/oauth/"):
                return self._oauth_post(u, raw)
            if u.path == "/mcp":
                return self._mcp(raw)
            if u.path.startswith("/console/v1/upload-sessions/"):
                return self._upload_session(u, raw)
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
                q = {"quoteId": f"q_{credits}_{uuid.uuid4().hex[:6]}", "model": body["model"], "estimatedCost": usd(credits), "maxCharge": usd(credits), "maxCredits": credits, "expectedCredits": credits, "currency": "USD", "quantity": "1", "unit": "image", "expiresAt": "2099-01-01T00:00:00Z"}
                if not state.legacy:
                    q.update({"maxChargeUsd": usd(credits), "expectedChargeUsd": usd(credits), "expectedCharge": usd(credits)})
                return self._ok(q)
            if u.path == "/api/v1/jobs/createTask":
                key = self.headers.get("Idempotency-Key", "")
                with state.lock:
                    if key and key in state.idem:
                        t = state.tasks[state.idem[key]]
                        return self._ok(self._accepted(t["taskId"], t["state"], t["credits"]), 202)
                    state.last_create = body
                    credits = self._price(body)
                    if body.get("expectedCostUsd") is not None and Decimal(str(body["expectedCostUsd"])) != Decimal(credits) / 1000:
                        return self._err(409, 40901, "Price changed")
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
                return self._ok(self._accepted(tid, "queued", credits), 202)
            if u.path == "/api/v1/common/download-url":
                t = state.tasks.get(body.get("taskId", ""))
                if not t:
                    return self._err(404, 404, "Not found")
                ext = "mp4" if t["video"] else "png"
                return self._ok({"key": "out-1", "url": f"{self._base()}/blob/{t['taskId']}.{ext}", "expiresAt": "2099-01-01T00:00:00Z"})
            return self._err(404, 404, "Not found")

        # ---------------- account mode ----------------
        def _oauth_get(self, u):
            base = self._base()
            if u.path == "/.well-known/oauth-protected-resource/mcp":
                return self._send(200, {"resource": f"{base}/mcp", "authorization_servers": [base]})
            if u.path == "/.well-known/oauth-authorization-server":
                return self._send(200, {
                    "issuer": "https://evil.example" if state.bad_issuer else base,
                    "authorization_endpoint": f"{base}/oauth/authorize",
                    "token_endpoint": f"{base}/oauth/token",
                    "registration_endpoint": f"{base}/oauth/register",
                    "revocation_endpoint": f"{base}/oauth/revoke",
                })
            if u.path == "/oauth/authorize":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                ok = q.get("code_challenge_method") == "S256" and q.get("scope") == "generate read" and q.get("resource") == f"{base}/mcp" and q.get("response_type") == "code"
                if not ok or q.get("client_id") not in {r["client_id"] for r in state.registered}:
                    return self._send(400, {"error": "invalid_request"})
                code = "code-" + uuid.uuid4().hex[:8]
                state.codes[code] = {"challenge": q.get("code_challenge"), "redirect_uri": q.get("redirect_uri"), "client_id": q.get("client_id")}
                back = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", ""), "iss": base})
                self.send_response(302)
                self.send_header("Location", back)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send(404, {"error": "not_found"})

        def _oauth_post(self, u, raw: bytes):
            if u.path == "/oauth/register":
                reg = json.loads(raw or b"{}")
                reg["client_id"] = f"pnc_{len(state.registered) + 1}"
                state.registered.append(reg)
                return self._send(201, {"client_id": reg["client_id"], "redirect_uris": reg.get("redirect_uris")})
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            if u.path == "/oauth/token":
                if form.get("grant_type") == "authorization_code":
                    c = state.codes.pop(form.get("code", ""), None)
                    verifier = form.get("code_verifier", "").encode()
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
                    if not c or c["challenge"] != challenge or c["redirect_uri"] != form.get("redirect_uri") or c["client_id"] != form.get("client_id"):
                        return self._send(400, {"error": "invalid_grant"})
                    state.tokens["issued"] += 1
                    at, rt = f"at-{state.tokens['issued']}", f"rt-{state.tokens['issued']}"
                    state.valid_tokens.add(at)
                    state.refresh_tokens.add(rt)
                    return self._send(200, {"access_token": at, "refresh_token": rt, "expires_in": 3600, "scope": "generate read", "token_type": "Bearer"})
                if form.get("grant_type") == "refresh_token":
                    if form.get("refresh_token") not in state.refresh_tokens:
                        return self._send(400, {"error": "invalid_grant"})
                    state.tokens["refreshed"] += 1
                    at = f"at-r{state.tokens['refreshed']}"
                    state.valid_tokens.add(at)
                    return self._send(200, {"access_token": at, "expires_in": 3600, "token_type": "Bearer"})
                return self._send(400, {"error": "unsupported_grant_type"})
            if u.path == "/oauth/revoke":
                tok = form.get("token", "")
                state.tokens["revoked"].append(tok)
                state.valid_tokens.discard(tok)
                state.refresh_tokens.discard(tok)
                return self._send(200, b"", "text/plain")
            return self._send(404, {"error": "not_found"})

        def _rpc(self, rid, result: dict, as_sse: bool):
            msg = {"jsonrpc": "2.0", "id": rid, "result": result}
            if as_sse:
                return self._send(200, f"event: message\ndata: {json.dumps(msg)}\n\n".encode(), "text/event-stream")
            return self._send(200, msg)

        def _mcp(self, raw: bytes):
            auth = self.headers.get("Authorization", "")
            tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if state.expire_token and tok == state.expire_token:
                state.expire_token = ""
                state.valid_tokens.discard(tok)
                return self._send(401, {"error": "invalid_token"}, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
            if tok not in state.valid_tokens:
                return self._send(401, {"error": "invalid_token"})
            msg = json.loads(raw or b"{}")
            method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
            if rid is None:  # a notification
                state.mcp_calls.append({"method": method})
                return self._send(202, b"", "text/plain")
            if method == "initialize":
                state.mcp_calls.append({"method": method})
                return self._rpc(rid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "popnow", "version": "test"}}, False)
            if method == "tools/list":
                return self._rpc(rid, {"tools": [{"name": n} for n in ("check_balance", "estimate_cost", "edit_image", "edit_video", "copy_motion", "create_image", "get_job", "request_upload", "get_upload")]}, state.sse)
            name, args = params.get("name"), params.get("arguments") or {}
            state.mcp_calls.append({"method": method, "name": name, "args": args, "auth": tok})
            with state.lock:
                result = self._tool(name, args)
            return self._rpc(rid, result, state.sse)

        def _text(self, text: str, data: dict | None = None, error: bool = False) -> dict:
            out: dict = {"content": [{"type": "text", "text": text}]}
            if data is not None:
                out["structuredContent"] = data
            if error:
                out["isError"] = True
            return out

        def _tool(self, name: str, args: dict) -> dict:
            base = self._base()
            if name == "check_balance":
                return self._text(f"You have {state.credits} PopNow credits.", {"currency": "credits", "credits": state.credits, "held": 0})
            if name == "estimate_cost":
                tool = args.get("tool")
                if tool not in state.prices:
                    return self._text("I can't price that.", error=True)
                n = state.prices[tool] - state.estimate_offset
                return self._text(f"{tool} would cost about {n} credits.", {"currency": "credits", "credits": n, "balance": state.credits, "steps": [n]})
            if name == "request_upload":
                tok = "tok" + uuid.uuid4().hex[:8]
                uid = "up_" + tok[3:]
                state.sessions[tok] = {"upload_id": uid, "files": [], "closed": False, "kinds": args.get("kinds"), "max": args.get("max_files")}
                return self._text("Upload page ready.", {"upload_id": uid, "url": f"https://popnow.ai/u/{tok}"})
            if name == "get_upload":
                sess = next((x for x in state.sessions.values() if x["upload_id"] == args.get("upload_id")), None)
                if sess is None:
                    return self._text("No such upload.", error=True)
                files = [{"popnow_link": f"popnow://f/{f['fileId']}", "name": f["name"], "type": f["contentType"]} for f in sess["files"] if f.get("committed")]
                return self._text(f"{len(files)} file(s) uploaded.", {"upload_id": sess["upload_id"], "state": "closed" if sess["closed"] else "open", "files": files})
            if name in state.prices:
                return self._paid(name, args, base)
            if name == "get_job":
                j = state.account_jobs.get(args.get("job_id", ""))
                if not j:
                    return self._text("I can't find that creation.", error=True)
                j["polls"] += 1
                if j["polls"] >= 2 and j["status"] in ("queued", "running"):
                    j["status"] = "failed" if j["fail"] else "succeeded"
                    if j["fail"]:
                        state.credits += j["credits"]
                elif j["polls"] == 1:
                    j["status"] = "running"
                return self._job_result(j, base)
            return self._text(f"Unknown tool {name}.", error=True)

        def _paid(self, name: str, args: dict, base: str) -> dict:
            for key in ("image_url", "video_url"):
                v = args.get(key)
                if v is not None and not str(v).startswith(("popnow://f/", "https://")):
                    return self._text(f"Bad link in {key}.", error=True)
            if name in ("edit_image", "edit_video", "create_image") and not str(args.get("prompt") or "").strip():
                return self._text("Describe what you want first.", error=True)
            if "model" in args:
                return self._text("This test server expects no model.", error=True)
            price = state.prices[name] + state.bump_once
            if name in state.consent_tools and args.get("consent") is not True:
                return self._text("Needs consent.", {"currency": "credits", "needs_confirmation": True, "needs_consent": True, "credits": price, "balance": state.credits})
            mc = int(args.get("max_credits") or 0)
            if mc < price:
                if mc > 0:
                    state.bump_once = 0
                    return self._text(f"Not started: the price is now {price} credits, more than the {mc} the user agreed to. Nothing was charged.")
                return self._text(f"Needs confirmation, nothing started yet. It costs {price} credits.", {"currency": "credits", "needs_confirmation": True, "credits": price, "balance": state.credits})
            if price > state.credits:
                return self._text("Not enough credits. Nothing was charged.", error=True)
            state.credits -= price
            jid = "job_" + uuid.uuid4().hex[:12]
            j = {"job_id": jid, "tool": name, "status": "queued", "credits": price, "polls": 0, "fail": "FAIL" in str(args.get("prompt", "")), "video": name in ("edit_video", "copy_motion"), "args": args}
            state.account_jobs[jid] = j
            return self._job_result(j, base)

        def _job_result(self, j: dict, base: str) -> dict:
            data = {"job_id": j["job_id"], "status": j["status"], "credits": j["credits"], "model": "Auto", "currency": "credits"}
            if j["status"] == "succeeded":
                ext, mime = ("mp4", "video/mp4") if j["video"] else ("png", "image/png")
                data["files"] = [{"url": f"{base}/blob/{j['job_id']}.{ext}", "popnow_link": f"popnow://g/{j['job_id']}", "type": mime}]
                return self._text(f"Done! Open it: {data['files'][0]['url']}", data)
            if j["status"] == "failed":
                data["credits"] = 0
                return self._text("This one didn't work: The model declined this request.\nThe credits for it were refunded automatically.", data)
            return self._text(f"Started! job_id: {j['job_id']}", data)

        def _upload_session(self, u, raw: bytes):
            state.upload_auth.append(self.headers.get("Authorization", ""))
            parts = u.path[len("/console/v1/upload-sessions/"):].split("/")
            sess = state.sessions.get(parts[0])
            if sess is None or sess["closed"]:
                return self._err(404, 404, "Upload window not found")
            rest = parts[1:]
            if rest == ["files"]:
                body = json.loads(raw or b"{}")
                if len(sess["files"]) >= int(sess.get("max") or 10):
                    return self._err(400, 400, "Too many files")
                fid = "fil_" + uuid.uuid4().hex[:12]
                f = {"fileId": fid, "name": body["name"], "contentType": body["contentType"], "bytes": body["bytes"], "uploaded": False, "session": parts[0]}
                state.files[fid] = f
                sess["files"].append(f)
                return self._ok({"fileId": fid, "uploadUrl": f"{self._base()}/put/{fid}", "method": "PUT", "headers": {"Content-Type": body["contentType"], "X-Fake-Upload": "yes"}})
            if len(rest) == 3 and rest[0] == "files" and rest[2] == "commit":
                f = state.files.get(rest[1])
                if not f or not f["uploaded"]:
                    return self._err(409, 40003, "Upload incomplete")
                f["committed"] = True
                return self._ok({"fileId": f["fileId"], "state": "ready"})
            if rest == ["close"]:
                sess["closed"] = True
                return self._ok({"state": "closed"})
            return self._err(404, 404, "Not found")

        def _accepted(self, tid: str, task_state: str, credits: int) -> dict:
            out = {"taskId": tid, "state": task_state, "estimatedCost": usd(credits), "estimatedCredits": credits, "deadlineAt": "2099-01-01T00:00:00Z"}
            if not state.legacy:
                out["estimatedCostUsd"] = usd(credits)
            return out

        def _price(self, body: dict) -> int:
            m = body.get("model", "")
            if "reference-to-video" in m or "video-edit" in m or "character-animation" in m:
                return 110
            if "turbo" in m:
                return 15
            return 36

        def _record(self, t: dict) -> dict:
            if t["polls"] >= 2 and t["state"] in ("queued", "running"):
                t["state"] = "failed" if t["fail"] else "succeeded"
            elif t["polls"] == 1:
                t["state"] = "running"
            rec = {"taskId": t["taskId"], "model": t["model"], "state": t["state"], "cost": usd(t["credits"]), "credits": t["credits"], "exactCredits": str(t["credits"]), "settled": False, "createdAt": "2026-10-16T00:00:00Z", "deadlineAt": "2099-01-01T00:00:00Z"}
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
                    rec["exactCredits"] = "0"
                    rec["cost"] = "0"
                    if not t.get("refunded"):
                        t["refunded"] = True
                        state.balance += t["credits"]
            if not state.legacy:
                rec["costUsd"] = rec["cost"]
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
