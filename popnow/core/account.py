"""Sign in with a PopNow account and talk to the account MCP server (standard library only, no bpy).

This is the account mode of the add-on: no API key, the user's PopNow credits pay for each generation, the same balance
as on popnow.ai. The flow matches the PopNow CLI (`popnow login`):

- OAuth 2.1 authorization code with PKCE (S256), a dynamically registered public client and a loopback redirect
  (RFC 8252): an `http.server` on 127.0.0.1 with a random port, in a thread;
- discovery reads the MCP endpoint's protected-resource metadata, then the authorization server's metadata; the issuer must
  match and every endpoint must use https (except a server on this computer, for local testing);
- the callback must carry the state we sent and, when present, the expected issuer; a reply that does not match gets a 400
  and the wait goes on;
- the session (tokens) is a JSON file with mode 0600, written to a temporary file and renamed into place. The UI passes the
  path (the add-on's extension_path_user folder); tokens never go into userpref.blend, logs or the console;
- MCP calls are Streamable HTTP (stateless): initialize -> notifications/initialized -> tools/call, with replies as JSON or
  as a server-sent event stream; a 401 refreshes the token once;
- local files go through an upload window (request_upload -> upload-sessions -> get_upload); the signed storage URL never
  gets the Authorization header.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from .client import IMAGE_MAX_BYTES, MEDIA_MAX_BYTES, NetworkError, Transport, content_type_for

DEFAULT_MCP_URL = "https://api.popnow.ai/mcp"
SCOPES = "generate read"
CLIENT_NAME = "PopNow for Blender"
PROTOCOL_VERSION = "2025-06-18"
LOGIN_TIMEOUT_S = 5 * 60
# Creation tools wait up to about 40 seconds on the server before handing back a job id
TOOL_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 10 * 60.0
# Refresh a little before the access token runs out
EXPIRY_MARGIN_S = 30.0
PRICING_URL = "https://popnow.ai/pricing"
LIBRARY_URL = "https://popnow.ai/assets"


class AccountError(Exception):
    """A problem with signing in or with the account server, as one English sentence for the UI"""


class SignInRequired(AccountError):
    def __init__(self, message: str = "Sign in to PopNow first (Preferences > Add-ons > PopNow).") -> None:
        super().__init__(message)


# ---------------- session file ----------------


@dataclass
class Session:
    """What signing in keeps on disk. Field names on disk match the PopNow CLI's account.json (expiresAt in Unix ms)"""

    issuer: str
    mcp_url: str
    token_endpoint: str
    client_id: str
    access_token: str
    refresh_token: str = ""
    revocation_endpoint: str = ""
    expires_at: float | None = None  # Unix seconds
    scope: str = ""

    _DISK = {
        "issuer": "issuer",
        "mcp_url": "mcpUrl",
        "token_endpoint": "tokenEndpoint",
        "client_id": "clientId",
        "access_token": "accessToken",
        "refresh_token": "refreshToken",
        "revocation_endpoint": "revocationEndpoint",
        "scope": "scope",
    }

    def to_disk(self) -> dict:
        out = {disk: getattr(self, attr) for attr, disk in self._DISK.items() if getattr(self, attr)}
        if self.expires_at is not None:
            out["expiresAt"] = int(self.expires_at * 1000)
        return out

    @classmethod
    def from_disk(cls, d: Any) -> "Session | None":
        if not isinstance(d, dict):
            return None
        kw: dict[str, Any] = {}
        for attr, disk in cls._DISK.items():
            v = d.get(disk)
            if v is not None and not isinstance(v, str):
                return None
            kw[attr] = v or ""
        if not all(kw[k] for k in ("issuer", "mcp_url", "token_endpoint", "client_id", "access_token")):
            return None
        exp = d.get("expiresAt")
        kw["expires_at"] = float(exp) / 1000 if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
        return cls(**kw)

    def __repr__(self) -> str:  # never print tokens
        return f"Session(issuer={self.issuer!r}, mcp_url={self.mcp_url!r}, client_id={self.client_id!r})"


def read_session(path: str) -> Session | None:
    """None when signed out; a damaged file also counts as signed out (signing in writes a fresh one)"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return Session.from_disk(data)


def write_session(path: str, session: Session) -> None:
    """A private file (0600) in a private folder, written next to the target and renamed into place"""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".account-", suffix=".tmp", dir=folder)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # Windows has no fchmod; the user profile folder is private there
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session.to_disk(), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def delete_session(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


class SessionStore:
    """The session shared by every thread (sign-in, balance, each task). One lock, so two tasks that see a 401 at the
    same time refresh once, not twice with the same refresh token"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._loaded = False
        self._session: Session | None = None

    def get(self) -> Session | None:
        with self._lock:
            if not self._loaded:
                self._session = read_session(self.path)
                self._loaded = True
            return self._session

    def set(self, session: Session) -> None:
        with self._lock:
            write_session(self.path, session)
            self._session, self._loaded = session, True

    def clear(self) -> Session | None:
        with self._lock:
            old = self.get()
            delete_session(self.path)
            self._session, self._loaded = None, True
            return old

    def renew(self, failed_access_token: str, http: "Http", now: Callable[[], float] = time.time) -> Session:
        """Refresh the token, unless another thread already replaced the one that failed"""
        with self._lock:
            current = self.get()
            if current is None:
                raise SignInRequired()
            if current.access_token != failed_access_token:
                return current
            try:
                fresh = refresh(http, current, now())
            except (AccountError, NetworkError) as e:
                if isinstance(e, NetworkError):
                    raise
                raise SignInRequired(f"Your PopNow sign-in expired. Sign in again ({e}).") from e
            self.set(fresh)
            return fresh


# ---------------- HTTP helpers ----------------


class Http:
    """JSON and form requests over the add-on's Transport (certifi roots via client._ssl_context, no proxy for 127.0.0.1)"""

    def __init__(self, transport: Transport | None = None, user_agent: str = "PopNow-Blender") -> None:
        self.transport = transport or Transport(timeout=REQUEST_TIMEOUT_S)
        self.user_agent = user_agent

    def with_timeout(self, seconds: float) -> "Http":
        t = Transport(timeout=seconds, context=self.transport.context)
        return Http(t, self.user_agent)

    def send(self, method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        h = {"User-Agent": self.user_agent}
        h.update(headers or {})
        return self.transport.send(method, url, h, body)

    def get_json(self, url: str) -> dict:
        status, _, raw = self.send("GET", url, {"Accept": "application/json"})
        if status >= 300:
            raise AccountError(f"The PopNow sign-in server answered {status}.")
        return _json_object(raw)

    def post_json(self, url: str, value: Any, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(headers or {})
        return self.send("POST", url, h, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def post_form(self, url: str, form: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        return self.send("POST", url, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, urllib.parse.urlencode(form).encode("ascii"))


def _json_object(raw: bytes) -> dict:
    try:
        v = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        v = {}
    return v if isinstance(v, dict) else {}


def _is_loopback(host: str | None) -> bool:
    return (host or "").lower() in ("127.0.0.1", "localhost", "::1")


def require_secure(raw: str, what: str) -> str:
    """Only https, except a server on this computer (local testing)"""
    u = urllib.parse.urlparse(raw or "")
    if not u.netloc or not (u.scheme == "https" or (u.scheme == "http" and _is_loopback(u.hostname))):
        raise AccountError(f"{what} must use https: {raw}")
    return raw


def _origin(url: str) -> str:
    u = urllib.parse.urlparse(url)
    return f"{u.scheme}://{u.netloc}"


# ---------------- OAuth ----------------


@dataclass
class AuthServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str = ""


def _str(meta: dict, key: str) -> str:
    v = meta.get(key)
    if not isinstance(v, str) or not v:
        raise AccountError(f"The PopNow sign-in server did not return {key.replace('_', ' ')}.")
    return v


def discover(http: Http, mcp_url: str) -> AuthServer:
    """Find the authorization server from the MCP endpoint's protected-resource metadata (RFC 9728)"""
    require_secure(mcp_url, "The PopNow server")
    u = urllib.parse.urlparse(mcp_url)
    path = "" if u.path in ("", "/") else u.path
    meta = http.get_json(f"{_origin(mcp_url)}/.well-known/oauth-protected-resource{path}")
    servers = meta.get("authorization_servers")
    issuer = servers[0] if isinstance(servers, list) and servers and isinstance(servers[0], str) else _origin(mcp_url)
    require_secure(issuer, "The PopNow sign-in server")
    am = http.get_json(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server")
    declared = _str(am, "issuer")
    if declared.rstrip("/") != issuer.rstrip("/"):
        raise AccountError("The PopNow sign-in server's details do not match it (issuer mismatch).")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        require_secure(_str(am, key), "The PopNow sign-in server")
    revocation = am.get("revocation_endpoint")
    if isinstance(revocation, str) and revocation:
        require_secure(revocation, "The PopNow sign-in server")
    else:
        revocation = ""
    return AuthServer(declared, am["authorization_endpoint"], am["token_endpoint"], am["registration_endpoint"], revocation)


def register_client(http: Http, server: AuthServer, redirect_uri: str, client_name: str = CLIENT_NAME) -> str:
    status, _, raw = http.post_json(
        server.registration_endpoint,
        {
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPES,
        },
    )
    if status >= 300:
        raise AccountError(f"PopNow could not register this add-on for sign-in ({status}).")
    return _str(_json_object(raw), "client_id")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)"""
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(server: AuthServer, *, client_id: str, redirect_uri: str, state: str, challenge: str, resource: str) -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
    )
    sep = "&" if urllib.parse.urlparse(server.authorization_endpoint).query else "?"
    return f"{server.authorization_endpoint}{sep}{q}"


_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>PopNow</title><style>body{{font:16px/1.5 system-ui,sans-serif;background:#FAF4E8;color:#352242;display:grid;"
    "place-items:center;min-height:100vh;margin:0}}main{{max-width:28rem;padding:24px;text-align:center}}h1{{font-size:22px;margin:0 0 8px}}</style>"
    "</head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"
)


class Loopback:
    """A one-shot HTTP listener on 127.0.0.1 with a random port for the OAuth redirect, served from a thread"""

    def __init__(self) -> None:
        self._expected: tuple[str, str] | None = None
        self._done = threading.Event()
        self._code: str | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # the request line holds the code: never log it
                pass

            def _page(self, status: int, ok: bool, text: str) -> None:
                body = _PAGE.format(title="Signed in to PopNow" if ok else "Sign-in did not finish", text=text).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                owner._handle(self)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}/callback"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="popnow-login")
        self._thread.start()

    def _handle(self, h: Any) -> None:
        u = urllib.parse.urlparse(h.path)
        with self._lock:
            if u.path != "/callback" or self._expected is None or self._done.is_set():
                h.send_response(404)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
            state, issuer = self._expected
            if q.get("state") != state:
                h._page(400, False, "This sign-in link is not the one Blender is waiting for.")
                return
            iss = q.get("iss")
            if iss is not None and iss.rstrip("/") != issuer.rstrip("/"):
                h._page(400, False, "The reply came from an unexpected server.")
                return
            err, code = q.get("error"), q.get("code")
            if err or not code:
                self._error = "Sign-in was canceled." if err == "access_denied" else f"Sign-in failed ({err or 'no code in the reply'})."
                self._done.set()
                h._page(400, False, "Go back to Blender and click Sign in with PopNow again.")
                return
            self._code = code
            self._done.set()
            h._page(200, True, "You can close this tab and go back to Blender.")

    def expect(self, state: str, issuer: str) -> None:
        with self._lock:
            self._expected = (state, issuer)

    def wait(self, timeout_s: float = LOGIN_TIMEOUT_S, should_stop: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic) -> str:
        deadline = clock() + timeout_s
        while not self._done.wait(0.2):
            if should_stop and should_stop():
                raise AccountError("Sign-in was canceled.")
            if clock() > deadline:
                raise AccountError("Sign-in timed out. Click Sign in with PopNow again.")
        if self._error:
            raise AccountError(self._error)
        assert self._code is not None
        return self._code

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _token_request(http: Http, endpoint: str, form: dict[str, str]) -> dict:
    status, _, raw = http.post_form(endpoint, form)
    reply = _json_object(raw)
    if status >= 300 or not isinstance(reply.get("access_token"), str) or not reply.get("access_token"):
        reason = reply.get("error") if isinstance(reply.get("error"), str) else str(status)
        raise AccountError(f"The PopNow sign-in server refused the request ({reason}).")
    return reply


def _session_from(reply: dict, base: Session, now: float) -> Session:
    exp = reply.get("expires_in")
    s = Session(
        issuer=base.issuer,
        mcp_url=base.mcp_url,
        token_endpoint=base.token_endpoint,
        client_id=base.client_id,
        access_token=reply["access_token"],
        refresh_token=reply["refresh_token"] if isinstance(reply.get("refresh_token"), str) and reply["refresh_token"] else base.refresh_token,
        revocation_endpoint=base.revocation_endpoint,
        expires_at=now + float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None,
        scope=reply["scope"] if isinstance(reply.get("scope"), str) else base.scope,
    )
    return s


def login(
    http: Http,
    mcp_url: str,
    *,
    open_url: Callable[[str], Any],
    should_stop: Callable[[], bool] | None = None,
    timeout_s: float = LOGIN_TIMEOUT_S,
    now: Callable[[], float] = time.time,
) -> Session:
    """The whole sign-in: discover, start the loopback, register, open the browser, wait for the code, exchange it.
    open_url gets the sign-in link (the UI opens it and keeps it so the user can copy it)."""
    server = discover(http, mcp_url)
    loop = Loopback()
    try:
        client_id = register_client(http, server, loop.redirect_uri)
        verifier, challenge = pkce_pair()
        state = _b64url(secrets.token_bytes(16))
        url = authorize_url(server, client_id=client_id, redirect_uri=loop.redirect_uri, state=state, challenge=challenge, resource=mcp_url)
        loop.expect(state, server.issuer)
        open_url(url)
        code = loop.wait(timeout_s, should_stop)
        reply = _token_request(
            http,
            server.token_endpoint,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": loop.redirect_uri, "client_id": client_id, "code_verifier": verifier, "resource": mcp_url},
        )
        base = Session(server.issuer, mcp_url, server.token_endpoint, client_id, "", revocation_endpoint=server.revocation_endpoint)
        return _session_from(reply, base, now())
    finally:
        loop.close()


def refresh(http: Http, session: Session, now: float | None = None) -> Session:
    if not session.refresh_token:
        raise AccountError("no refresh token")
    reply = _token_request(http, session.token_endpoint, {"grant_type": "refresh_token", "refresh_token": session.refresh_token, "client_id": session.client_id, "resource": session.mcp_url})
    return _session_from(reply, session, time.time() if now is None else now)


def revoke(http: Http, session: Session) -> bool:
    """Best effort: a failed revocation never blocks signing out locally"""
    if not session.revocation_endpoint:
        return False
    ok = True
    for token, hint in ((session.refresh_token, "refresh_token"), (session.access_token, "access_token")):
        if not token:
            continue
        try:
            status, _, _ = http.post_form(session.revocation_endpoint, {"token": token, "token_type_hint": hint, "client_id": session.client_id})
            ok = ok and status < 300
        except NetworkError:
            ok = False
    return ok


# ---------------- MCP ----------------


@dataclass
class ToolResult:
    content: list
    structured: dict
    is_error: bool

    @property
    def text(self) -> str:
        return "\n".join(str(c.get("text") or "") for c in self.content if isinstance(c, dict) and c.get("type") == "text").strip()

    @property
    def needs_confirmation(self) -> bool:
        return self.structured.get("needs_confirmation") is True

    @property
    def needs_consent(self) -> bool:
        return self.structured.get("needs_consent") is True

    @property
    def credits(self) -> int | None:
        c = self.structured.get("credits")
        return int(c) if isinstance(c, (int, float)) and not isinstance(c, bool) and c >= 0 else None


def read_reply(headers: dict[str, str], raw: bytes, want_id: int) -> dict:
    """The JSON-RPC reply with this id, from a JSON body or a server-sent event stream"""
    text = raw.decode("utf-8", errors="replace")
    messages: list = []
    if "text/event-stream" in (headers.get("content-type") or ""):
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
            if data:
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    continue
    elif text.strip():
        try:
            parsed = json.loads(text)
        except ValueError as e:
            raise AccountError("The PopNow server sent a reply this add-on cannot read.") from e
        messages.extend(parsed if isinstance(parsed, list) else [parsed])
    reply = next((m for m in messages if isinstance(m, dict) and m.get("id") == want_id), None)
    if reply is None:
        raise AccountError("The PopNow server sent no reply.")
    err = reply.get("error")
    if isinstance(err, dict):
        raise AccountError(str(err.get("message") or "The PopNow server returned an error."))
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


class AccountClient:
    """A small Streamable HTTP MCP client. One per task or background call; the session lives in the shared SessionStore"""

    def __init__(self, store: SessionStore, *, http: Http | None = None, client_version: str = "0", now: Callable[[], float] = time.time) -> None:
        self.store = store
        self.http = http or Http()
        self.client_version = client_version
        self._now = now
        self._initialized = False
        self._session_id = ""
        self._protocol = PROTOCOL_VERSION
        self._next_id = 1
        self._lock = threading.Lock()

    @property
    def mcp_url(self) -> str:
        s = self.store.get()
        if s is None:
            raise SignInRequired()
        return s.mcp_url

    def _session(self) -> Session:
        s = self.store.get()
        if s is None:
            raise SignInRequired()
        return s

    def _post(self, body: dict, timeout_s: float) -> tuple[dict[str, str], bytes]:
        s = self._session()
        if s.expires_at is not None and s.expires_at - EXPIRY_MARGIN_S < self._now() and s.refresh_token:
            s = self.store.renew(s.access_token, self.http, self._now)
        http = self.http.with_timeout(timeout_s)
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        for attempt in (0, 1):
            h = {
                "Authorization": f"Bearer {s.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": self._protocol,
            }
            if self._session_id:
                h["Mcp-Session-Id"] = self._session_id
            status, rh, raw = http.send("POST", s.mcp_url, h, payload)
            if status == 401 and attempt == 0:
                s = self.store.renew(s.access_token, self.http, self._now)
                continue
            if status == 401:
                raise SignInRequired("Your PopNow sign-in expired. Sign in again.")
            if status == 429:
                raise AccountError("Too many requests right now. Wait a moment and try again.")
            if status >= 500:
                raise AccountError(f"PopNow is busy or unavailable right now ({status}). Try again in a minute.")
            if status >= 300:
                raise AccountError(f"The PopNow server answered {status}.")
            sid = rh.get("mcp-session-id")
            if sid:
                self._session_id = sid
            return rh, raw
        raise SignInRequired()

    def _request(self, method: str, params: dict, timeout_s: float = REQUEST_TIMEOUT_S) -> dict:
        if not self._initialized and method != "initialize":
            self._initialize()
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        rh, raw = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout_s)
        return read_reply(rh, raw, rid)

    def _initialize(self) -> None:
        self._initialized = True
        try:
            res = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "popnow-blender", "version": self.client_version}})
            if isinstance(res.get("protocolVersion"), str) and res["protocolVersion"]:
                self._protocol = res["protocolVersion"]
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, REQUEST_TIMEOUT_S)
        except BaseException:
            self._initialized = False
            raise

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = ""
        while True:
            res = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in res.get("tools") or [] if isinstance(t, dict))
            cursor = res.get("nextCursor") if isinstance(res.get("nextCursor"), str) else ""
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict, timeout_s: float = TOOL_TIMEOUT_S) -> ToolResult:
        res = self._request("tools/call", {"name": name, "arguments": arguments}, timeout_s)
        content = res.get("content") if isinstance(res.get("content"), list) else []
        structured = res.get("structuredContent") if isinstance(res.get("structuredContent"), dict) else {}
        return ToolResult(content, structured, bool(res.get("isError")))

    # ---------- the calls the add-on makes ----------
    def balance(self) -> dict:
        """{"credits": available, "held": set aside for running creations} from check_balance's structured result"""
        r = self.call_tool("check_balance", {}, REQUEST_TIMEOUT_S)
        if r.is_error:
            raise AccountError(r.text or "Could not read your PopNow credits.")
        out = {}
        for key in ("credits", "held"):
            v = r.structured.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[key] = int(v)
        if "credits" not in out:
            raise AccountError("PopNow did not return your credits.")
        return out

    def estimate(self, tool: str, arguments: dict) -> ToolResult:
        """estimate_cost: free, starts nothing"""
        return self.call_tool("estimate_cost", {"tool": tool, "arguments": {k: v for k, v in arguments.items() if k != "max_credits"}}, REQUEST_TIMEOUT_S * 2)

    def get_job(self, job_id: str, wait_seconds: int = 0) -> ToolResult:
        return self.call_tool("get_job", {"job_id": job_id, "wait_seconds": int(wait_seconds)}, REQUEST_TIMEOUT_S + wait_seconds)


# ---------------- uploads ----------------


def _kind(content_type: str) -> str:
    for k in ("image", "video", "audio"):
        if content_type.startswith(k + "/"):
            return k
    raise AccountError(f"Only images, videos and audio can be uploaded (got {content_type}).")


def _envelope(status: int, raw: bytes) -> dict:
    body = _json_object(raw)
    if status >= 300 or body.get("code") != 200:
        msg = body.get("msg")
        if status == 429:
            raise AccountError("Too many uploads right now. Wait a minute and try again.")
        raise AccountError(f"Upload failed: {msg}" if isinstance(msg, str) and msg else f"Upload failed ({status}).")
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def upload_files(client: AccountClient, paths: list[str], *, note: str = "Files from Blender", should_stop: Callable[[], bool] | None = None, on_file: Callable[[int, int], None] | None = None, http: Http | None = None) -> list[str]:
    """Put local files into the signed-in account through an upload window; returns their popnow:// links, in order"""
    if not paths:
        return []
    if len(paths) > 10:
        raise AccountError("Upload at most 10 files at once.")
    types = [content_type_for(p) for p in paths]
    for p, t in zip(paths, types):
        limit = IMAGE_MAX_BYTES if t.startswith("image/") else MEDIA_MAX_BYTES
        if os.path.getsize(p) > limit:
            raise AccountError(f"{os.path.basename(p)} is too large to upload.")
    kinds = sorted({_kind(t) for t in types})
    opened = client.call_tool("request_upload", {"kinds": kinds, "max_files": len(paths), "note": note}, REQUEST_TIMEOUT_S)
    upload_id, page = opened.structured.get("upload_id"), opened.structured.get("url")
    if opened.is_error or not isinstance(upload_id, str) or not isinstance(page, str) or not upload_id or not page:
        raise AccountError(opened.text or "PopNow did not open an upload window.")
    token = [seg for seg in urllib.parse.urlparse(page).path.split("/") if seg][-1:]
    if not token:
        raise AccountError("PopNow did not open an upload window.")
    api = f"{_origin(client.mcp_url)}/console/v1/upload-sessions/{urllib.parse.quote(token[0], safe='')}"
    # The upload window's endpoints are public (the token in the path is the credential): no Authorization header
    plain = (http or client.http).with_timeout(REQUEST_TIMEOUT_S)
    put_http = plain.with_timeout(UPLOAD_TIMEOUT_S)

    def post(path: str, body: dict | None = None) -> dict:
        status, _, raw = plain.post_json(api + path, body if body is not None else {})
        return _envelope(status, raw)

    names: list[str] = []
    for i, (p, t) in enumerate(zip(paths, types)):
        if should_stop and should_stop():
            raise InterruptedError("upload stopped")
        with open(p, "rb") as f:
            data = f.read()
        name = os.path.basename(p)
        names.append(name)
        ticket = post("/files", {"contentType": t, "bytes": len(data), "name": name})
        url, fid = ticket.get("uploadUrl"), ticket.get("fileId")
        if not isinstance(url, str) or not isinstance(fid, str) or not url or not fid:
            raise AccountError("Upload failed: no upload address.")
        headers = {str(k): str(v) for k, v in (ticket.get("headers") or {}).items() if str(k).lower() not in ("content-length", "host", "authorization")}
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = t
        method = ticket.get("method") if isinstance(ticket.get("method"), str) and ticket.get("method") else "PUT"
        status, _, _ = put_http.transport.send(method, url, headers, data)
        if status >= 300:
            raise AccountError(f"Upload of {name} failed ({status}).")
        post(f"/files/{urllib.parse.quote(fid, safe='')}/commit")
        if on_file:
            on_file(i + 1, len(paths))
    try:
        post("/close")
    except (AccountError, NetworkError):
        pass
    got = client.call_tool("get_upload", {"upload_id": upload_id, "wait_seconds": 0}, REQUEST_TIMEOUT_S)
    listed = [x for x in (got.structured.get("files") or []) if isinstance(x, dict)]
    links: list[str] = []
    for name in names:
        at = next((i for i, x in enumerate(listed) if x.get("name") == name), None)
        item = listed.pop(at) if at is not None else (listed.pop(0) if listed else None)
        link = item.get("popnow_link") if item else None
        if not isinstance(link, str) or not link:
            raise AccountError(f"PopNow did not return a link for {name}.")
        links.append(link)
    return links


def credits_text(n: int | None) -> str:
    """ "1 credit", "1,250 credits"; "?" when unknown"""
    if n is None:
        return "?"
    return f"{n:,} credit" + ("" if n == 1 else "s")


__all__ = [
    "AccountClient",
    "AccountError",
    "AuthServer",
    "DEFAULT_MCP_URL",
    "Http",
    "Loopback",
    "Session",
    "SessionStore",
    "SignInRequired",
    "ToolResult",
    "authorize_url",
    "credits_text",
    "delete_session",
    "discover",
    "login",
    "pkce_pair",
    "read_session",
    "refresh",
    "register_client",
    "require_secure",
    "revoke",
    "upload_files",
    "write_session",
]
