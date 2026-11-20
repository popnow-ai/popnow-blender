"""Account mode without Blender: sign-in (OAuth + PKCE + loopback), the MCP client, upload windows and AccountJobRunner,
against the local fake server (tests/fake_server.py). No network, no cost.

Run (from the repository root): python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from popnow.core import account as A  # noqa: E402
from popnow.core.account_jobs import AccountJobRunner, tool_call  # noqa: E402
from popnow.core.client import NetworkError  # noqa: E402
from popnow.core.jobs import Job, MediaFile  # noqa: E402

import fake_server  # noqa: E402

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def follow(url: str) -> None:
    """The "browser": open the sign-in link, which redirects back to the add-on's loopback port"""
    with _NO_PROXY.open(url, timeout=10) as r:
        r.read()


def wait_for(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class AccountCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv, self.state = fake_server.start(0)
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.mcp = f"{self.origin}/mcp"
        self.tmp = tempfile.mkdtemp(prefix="popnow-account-")
        self.path = os.path.join(self.tmp, "ext", "account.json")
        self.http = A.Http()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sign_in(self) -> A.SessionStore:
        store = A.SessionStore(self.path)
        store.set(A.login(self.http, self.mcp, open_url=follow, timeout_s=10))
        return store

    def client(self, store: A.SessionStore | None = None) -> A.AccountClient:
        return A.AccountClient(store or self.sign_in(), http=self.http, client_version="test")

    def png(self, name: str) -> str:
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(fake_server.tiny_png(16, 9))
        return p

    def mp4(self, name: str = "clay.mp4") -> str:
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42clay")
        return p

    def paid_calls(self) -> list[dict]:
        return [c for c in self.state.mcp_calls if c.get("name") in self.state.prices]


class SignInTests(AccountCase):
    def test_signs_in_with_pkce_and_a_loopback_redirect_and_keeps_tokens_private(self):
        store = self.sign_in()
        reg = self.state.registered[0]
        self.assertRegex(reg["redirect_uris"][0], r"^http://127\.0\.0\.1:\d+/callback$")
        self.assertEqual(reg["token_endpoint_auth_method"], "none")
        self.assertEqual(reg["scope"], "generate read")
        self.assertEqual(reg["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["accessToken"], "at-1")
        self.assertEqual(saved["refreshToken"], "rt-1")
        self.assertEqual(saved["mcpUrl"], self.mcp)
        self.assertGreater(saved["expiresAt"], time.time() * 1000)
        # no temporary file left next to it, and tokens never show in a repr
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["account.json"])
        self.assertNotIn("at-1", repr(store.get()))
        self.assertEqual(A.read_session(self.path).access_token, "at-1")

    def test_callback_with_wrong_state_or_issuer_gets_400_and_the_wait_goes_on(self):
        loop = A.Loopback()
        try:
            loop.expect("good-state", self.origin)
            base = loop.redirect_uri
            for q in ("code=x&state=bad", f"code=x&state=good-state&iss=https%3A%2F%2Fevil.example"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    follow(f"{base}?{q}")
                self.assertEqual(cm.exception.code, 400)
            threading.Thread(target=lambda: follow(f"{base}?code=the-code&state=good-state&iss={self.origin}"), daemon=True).start()
            self.assertEqual(loop.wait(5), "the-code")
        finally:
            loop.close()

    def test_denied_and_timeout(self):
        loop = A.Loopback()
        try:
            loop.expect("s", self.origin)
            threading.Thread(target=lambda: self._quiet(f"{loop.redirect_uri}?error=access_denied&state=s"), daemon=True).start()
            with self.assertRaises(A.AccountError) as cm:
                loop.wait(5)
            self.assertIn("canceled", str(cm.exception))
        finally:
            loop.close()
        loop = A.Loopback()
        try:
            loop.expect("s", self.origin)
            with self.assertRaises(A.AccountError):
                loop.wait(0.3)
            stop = threading.Event()
            stop.set()
            with self.assertRaises(A.AccountError) as cm:
                loop.wait(5, should_stop=stop.is_set)
            self.assertIn("canceled", str(cm.exception))
        finally:
            loop.close()

    @staticmethod
    def _quiet(url: str) -> None:
        try:
            follow(url)
        except Exception:
            pass

    def test_discovery_checks_issuer_and_https(self):
        self.state.bad_issuer = True
        with self.assertRaises(A.AccountError) as cm:
            A.discover(self.http, self.mcp)
        self.assertIn("issuer", str(cm.exception))
        with self.assertRaises(A.AccountError):
            A.require_secure("http://api.popnow.ai/mcp", "The PopNow server")
        self.assertEqual(A.require_secure("http://localhost:9/mcp", "x"), "http://localhost:9/mcp")
        self.assertEqual(A.require_secure("https://api.popnow.ai/mcp", "x"), "https://api.popnow.ai/mcp")

    def test_authorize_url_carries_pkce_resource_and_scope(self):
        server = A.discover(self.http, self.mcp)
        verifier, challenge = A.pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        url = A.authorize_url(server, client_id="c", redirect_uri="http://127.0.0.1:1/callback", state="s", challenge=challenge, resource=self.mcp)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual((q["code_challenge_method"], q["scope"], q["resource"], q["response_type"]), ("S256", "generate read", self.mcp, "code"))

    def test_sign_out_revokes_and_deletes(self):
        store = self.sign_in()
        session = store.clear()
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(store.get())
        self.assertTrue(A.revoke(self.http, session))
        self.assertEqual(self.state.tokens["revoked"], ["rt-1", "at-1"])
        with self.assertRaises(A.SignInRequired):
            A.AccountClient(store, http=self.http).balance()

    def test_damaged_session_file_counts_as_signed_out(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(A.SessionStore(self.path).get())


class McpTests(AccountCase):
    def test_balance_over_sse_and_json(self):
        c = self.client()
        self.assertEqual(c.balance(), {"credits": 12000, "held": 0})
        self.state.sse = False
        self.assertEqual(c.balance()["credits"], 12000)
        methods = [x["method"] for x in self.state.mcp_calls]
        self.assertEqual(methods[:3], ["initialize", "notifications/initialized", "tools/call"])

    def test_read_reply_formats(self):
        sse = b'event: message\r\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":1}}\r\n\r\n'
        self.assertEqual(A.read_reply({"content-type": "text/event-stream"}, sse, 7), {"ok": 1})
        batch = b'[{"jsonrpc":"2.0","id":1,"result":{}},{"jsonrpc":"2.0","id":2,"result":{"x":2}}]'
        self.assertEqual(A.read_reply({"content-type": "application/json"}, batch, 2), {"x": 2})
        with self.assertRaises(A.AccountError):
            A.read_reply({"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":3,"error":{"message":"nope"}}', 3)

    def test_401_refreshes_once_and_saves_the_new_token(self):
        store = self.sign_in()
        c = self.client(store)
        c.balance()
        self.state.expire_token = "at-1"
        self.assertEqual(c.balance()["credits"], 12000)
        self.assertEqual(self.state.tokens["refreshed"], 1)
        saved = A.read_session(self.path)
        self.assertEqual(saved.access_token, "at-r1")
        self.assertEqual(saved.refresh_token, "rt-1")  # kept when the server sends no new one

    def test_refresh_before_expiry(self):
        store = self.sign_in()
        c = A.AccountClient(store, http=self.http, now=lambda: time.time() + 7200)
        c.balance()
        self.assertEqual(self.state.tokens["refreshed"], 1)

    def test_refused_refresh_means_sign_in_again(self):
        store = self.sign_in()
        self.state.refresh_tokens.clear()
        self.state.expire_token = "at-1"
        with self.assertRaises(A.SignInRequired):
            A.AccountClient(store, http=self.http).balance()

    def test_upload_window(self):
        c = self.client()
        links = A.upload_files(c, [self.png("clay.png"), self.png("ref1.png")])
        self.assertEqual(len(links), 2)
        self.assertTrue(all(x.startswith("popnow://f/fil_") for x in links))
        sess = next(iter(self.state.sessions.values()))
        self.assertEqual([f["name"] for f in sess["files"]], ["clay.png", "ref1.png"])
        self.assertEqual(links, [f"popnow://f/{f['fileId']}" for f in sess["files"]])
        self.assertTrue(sess["closed"])
        # the upload window and the signed storage URL never get the sign-in token
        self.assertTrue(self.state.upload_auth)
        self.assertEqual(set(self.state.upload_auth), {""})
        req = next(x for x in self.state.mcp_calls if x.get("name") == "request_upload")
        self.assertEqual(req["args"]["kinds"], ["image"])
        self.assertEqual(req["args"]["max_files"], 2)


class RunnerTests(AccountCase):
    def runner(self, job: Job, **kw) -> AccountJobRunner:
        self.events: list[str] = []
        r = AccountJobRunner(self.client(), job, lambda j: self.events.append(j.state), sleep=lambda s: time.sleep(0.005), **kw)
        r.start()
        return r

    def render_job(self, prompt: str = "The first image is a grey clay render") -> Job:
        media = [MediaFile(self.png("clay.png"), "structure"), MediaFile(self.png("depth.png"), "structure"), MediaFile(self.png("ref1.png"), "refs")]
        return Job(workflow="render", model="", label="Auto", prompt=prompt, user_prompt="cabin", options={"aspect_ratio": "16:9"}, media=media, out_dir=os.path.join(self.tmp, "out"), mode="account")

    def test_render_flow_quotes_confirms_and_downloads(self):
        job = self.render_job()
        r = self.runner(job)
        self.assertTrue(wait_for(lambda: job.state == "awaiting_confirm"))
        self.assertEqual(job.max_credits, 48)
        self.assertIn("48 credits", job.status)
        self.assertEqual(self.paid_calls(), [])  # only priced so far
        self.assertEqual(self.state.credits, 12000)
        r.confirm()
        r.join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertEqual(job.status, "Done · 48 credits")
        self.assertEqual(self.state.credits, 12000 - 48)
        call = self.paid_calls()[0]
        self.assertEqual(call["name"], "edit_image")
        args = call["args"]
        self.assertEqual(args["max_credits"], 48)
        self.assertEqual(args["aspect_ratio"], "16:9")
        self.assertNotIn("model", args)
        self.assertNotIn("consent", args)
        self.assertTrue(args["image_url"].startswith("popnow://f/"))
        self.assertEqual(len(args["more_image_urls"]), 2)
        est = next(x for x in self.state.mcp_calls if x.get("name") == "estimate_cost")
        self.assertEqual(est["args"]["tool"], "edit_image")
        self.assertNotIn("max_credits", est["args"]["arguments"])
        self.assertTrue(os.path.exists(job.outputs[0]))
        with open(job.outputs[0], "rb") as f:
            self.assertTrue(f.read().startswith(b"\x89PNG"))
        self.assertEqual(set(self.state.download_auth), {""})  # the download link gets no token
        self.assertTrue(job.task_id.startswith("job_"))
        self.assertIn("uploading", self.events)

    def test_declining_charges_nothing(self):
        job = self.render_job()
        r = self.runner(job)
        self.assertTrue(wait_for(lambda: job.state == "awaiting_confirm"))
        r.cancel()
        r.join(5)
        self.assertEqual(job.state, "canceled")
        self.assertIn("Nothing was charged", job.status)
        self.assertEqual(self.paid_calls(), [])
        self.assertEqual(self.state.credits, 12000)

    def test_threshold_starts_without_asking_only_when_the_price_fits(self):
        job = self.render_job()
        self.runner(job, auto_confirm_below_credits=50).join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertNotIn("awaiting_confirm", self.events)
        job2 = self.render_job()
        r = self.runner(job2, auto_confirm_below_credits=10)
        self.assertTrue(wait_for(lambda: job2.state == "awaiting_confirm"))
        r.cancel()
        r.join(5)

    def test_a_price_change_is_asked_again_and_confirmed_by_the_user(self):
        self.state.bump_once = 30  # the price goes up between the quote and the start, once
        job = self.render_job()
        r = self.runner(job, auto_confirm_below_credits=100)  # under the threshold: the first price starts by itself
        self.assertTrue(wait_for(lambda: len(self.paid_calls()) == 1 and job.state == "awaiting_confirm"))
        self.assertEqual(self.state.credits, 12000)  # refused, nothing charged
        time.sleep(0.2)
        self.assertEqual(len(self.paid_calls()), 1)  # a second price always waits for the user, threshold or not
        r.confirm()
        r.join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertEqual([c["args"]["max_credits"] for c in self.paid_calls()], [48, 48])
        self.assertEqual(self.state.credits, 12000 - 48)

    def test_max_credits_is_never_raised_on_the_users_behalf(self):
        self.state.estimate_offset = 10  # PopNow keeps asking for more than the user agreed to
        job = self.render_job()
        r = self.runner(job, auto_confirm_below_credits=100)
        confirms = 0
        deadline = time.monotonic() + 20
        while r.is_alive() and time.monotonic() < deadline:
            if job.state == "awaiting_confirm":
                n = len(self.paid_calls())
                r.confirm()
                confirms += 1
                wait_for(lambda: len(self.paid_calls()) > n or not r.is_alive())
            time.sleep(0.01)
        r.cancel()
        r.join(5)
        self.assertEqual(job.state, "failed", job.status)
        self.assertIn("Nothing was charged", job.status)
        self.assertEqual({c["args"]["max_credits"] for c in self.paid_calls()}, {38})
        self.assertEqual(len(self.paid_calls()), confirms + 1)  # every try after the first one waited for a click
        self.assertEqual(self.state.credits, 12000)

    def test_consent_is_sent_only_after_the_user_ticks_it(self):
        self.state.consent_tools.add("edit_image")
        job = self.render_job()
        r = self.runner(job)
        self.assertTrue(wait_for(lambda: job.state == "awaiting_confirm"))
        r.confirm()
        self.assertTrue(wait_for(lambda: job.extra.get("needs_consent") and job.state == "awaiting_confirm"))
        self.assertIn("permission", job.status)
        r.confirm()  # Start without ticking: still waiting, nothing sent
        time.sleep(0.2)
        self.assertEqual(job.state, "awaiting_confirm")
        self.assertEqual(len(self.paid_calls()), 1)
        r.confirm(consent=True)
        r.join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertNotIn("consent", self.paid_calls()[0]["args"])
        self.assertIs(self.paid_calls()[-1]["args"]["consent"], True)

    def test_failure_is_refunded(self):
        job = self.render_job(prompt="FAIL please")
        r = self.runner(job, auto_confirm_below_credits=100)
        r.join(10)
        self.assertEqual(job.state, "failed")
        self.assertIn("refunded", job.status)
        self.assertEqual(self.state.credits, 12000)

    def test_stop_waiting_then_resume(self):
        job = Job(workflow="texture", model="", label="Auto", prompt="Seamless tileable texture of moss.", user_prompt="moss", options={"aspect_ratio": "1:1"}, media=[], out_dir=os.path.join(self.tmp, "out"), mode="account")
        r = self.runner(job, auto_confirm_below_credits=100)
        self.assertTrue(wait_for(lambda: bool(job.task_id)))
        r.cancel()
        r.join(5)
        self.assertEqual(job.state, "stopped")
        self.assertTrue(job.resumable)
        call = self.paid_calls()[0]
        self.assertEqual((call["name"], call["args"]["aspect_ratio"]), ("create_image", "1:1"))
        job.state = "queued"
        AccountJobRunner(self.client(A.SessionStore(self.path)), job, lambda j: None, sleep=lambda s: None).run()
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertEqual(len(self.paid_calls()), 1)  # resuming never starts a second one

    def test_animate_uses_edit_video_or_copy_motion(self):
        clip = self.mp4()
        job = Job(workflow="animate", model="", label="Auto", prompt="Restyle this grey clay previz video.", user_prompt="", options={}, media=[MediaFile(clip, "clip", 5.0), MediaFile(self.png("r.png"), "refs")], out_dir=os.path.join(self.tmp, "out"), mode="account")
        self.assertEqual(tool_call(job, {"clip": ["popnow://f/c"], "refs": ["popnow://f/r"]}), ("edit_video", {"video_url": "popnow://f/c", "prompt": "Restyle this grey clay previz video.", "reference_image_urls": ["popnow://f/r"]}))
        self.runner(job, auto_confirm_below_credits=1000).join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertTrue(job.outputs[0].endswith(".mp4"))
        job2 = Job(workflow="animate", model="", label="Auto", prompt="", user_prompt="", options={}, media=[MediaFile(clip, "clip", 5.0), MediaFile(self.png("c.png"), "character")], out_dir=os.path.join(self.tmp, "out"), mode="account")
        self.assertEqual(tool_call(job2, {"clip": ["popnow://f/c"], "character": ["popnow://f/p"]}), ("copy_motion", {"video_url": "popnow://f/c", "image_url": "popnow://f/p"}))

    def test_network_error_before_start_charges_nothing(self):
        job = self.render_job()
        store = self.sign_in()
        self.srv.shutdown()
        self.srv.server_close()
        r = AccountJobRunner(A.AccountClient(store, http=A.Http()), job, lambda j: None, sleep=lambda s: None)
        r.run()
        self.assertEqual(job.state, "failed")
        self.assertIn("Nothing was charged", job.status)
        self.srv, self.state = fake_server.start(0)  # for tearDown

    def test_history_keeps_the_mode(self):
        job = self.render_job()
        job.max_credits, job.credits, job.task_id, job.state = 48, 48, "job_x", "running"
        back = Job.from_dict(json.loads(json.dumps(job.to_dict())))
        self.assertEqual(back.mode, "account")
        self.assertEqual(back.state, "stopped")
        self.assertEqual((back.max_credits, back.credits), (48, 48))
        old = Job.from_dict({k: v for k, v in job.to_dict().items() if k != "mode"})
        self.assertEqual(old.mode, "api")


if __name__ == "__main__":
    unittest.main()
