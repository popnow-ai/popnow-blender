"""Pure Python unit tests for the add-on core (no Blender needed).

Run (from the repository root): python3 -m unittest discover -s tests -v
The imaging tests need numpy (bundled with Blender); they are skipped automatically when the system Python has no numpy.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))  # make popnow.core importable (without importing the bpy part in popnow/__init__.py)
sys.path.insert(0, HERE)

from popnow.core import client as C  # noqa: E402
from popnow.core import history, models, recipes  # noqa: E402
from popnow.core.jobs import Job, JobRunner, MediaFile  # noqa: E402

import fake_server  # noqa: E402

KEY = fake_server.VALID_KEY_PREFIX + "0123456789abcdef0123456789"


class FakeServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv, self.state = fake_server.start(0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/api/v1"
        self.tmp = tempfile.mkdtemp(prefix="popnow-test-")
        self.client = C.PopNowClient(KEY, self.base, sleep=lambda s: None)

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def png(self, name: str = "clay.png") -> str:
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(fake_server.tiny_png(16, 9))
        return p


class ClientTests(FakeServerCase):
    def test_balance(self):
        b = self.client.balance()
        self.assertEqual(b["availableCredits"], 5000)

    def test_bad_key_gives_friendly_401(self):
        bad = C.PopNowClient("sk-popnow-wrong", self.base, sleep=lambda s: None)
        with self.assertRaises(C.ApiError) as cm:
            bad.balance()
        self.assertEqual(cm.exception.http, 401)
        self.assertIn("API key was not accepted", str(cm.exception))

    def test_upload_three_steps(self):
        res = self.client.upload_file(self.png())
        self.assertTrue(res["uri"].startswith("popnow://f/fil_"))
        self.assertEqual([c for c in self.state.calls if c[0] != "GET"][:3], [("POST", "/api/v1/common/upload-url"), ("PUT", f"/put/{res['fileId']}"), ("POST", f"/api/v1/files/{res['fileId']}/commit")])

    def test_quote_retries_on_503(self):
        self.state.fail_quote_once = True
        q = self.client.quote("bytedance/seedream-5.0-pro/edit", {"prompt": "x", "image_urls": ["https://example.com/a.png"]})
        self.assertEqual(q["maxCredits"], 36)

    def test_create_task_is_idempotent(self):
        body = ("bytedance/seedream-5.0-pro/edit", {"prompt": "x", "image_urls": ["https://example.com/a.png"]})
        a = self.client.create_task(*body, expected_credits=36, idempotency_key="blender-same")
        b = self.client.create_task(*body, expected_credits=36, idempotency_key="blender-same")
        self.assertEqual(a["taskId"], b["taskId"])
        self.assertEqual(self.state.balance, 4964)

    def test_price_change_is_40901(self):
        with self.assertRaises(C.ApiError) as cm:
            self.client.create_task("bytedance/seedream-5.0-pro/edit", {"prompt": "x"}, expected_credits=1)
        self.assertEqual(cm.exception.code, 40901)

    def test_insufficient_credits_message(self):
        self.state.balance = 1
        with self.assertRaises(C.ApiError) as cm:
            self.client.create_task("bytedance/seedream-5.0-pro/edit", {"prompt": "x"})
        self.assertIn("Not enough credits", str(cm.exception))

    def test_network_error(self):
        c = C.PopNowClient(KEY, "http://127.0.0.1:9/api/v1", sleep=lambda s: None)
        c.transport.timeout = 2
        with self.assertRaises(C.NetworkError):
            c.balance()

    def test_helpers(self):
        self.assertEqual(C.content_type_for("/a/b.PNG"), "image/png")
        self.assertEqual(C.content_type_for("clip.mp4"), "video/mp4")
        with self.assertRaises(ValueError):
            C.content_type_for("x.exr")
        self.assertEqual(C.mask_key("sk-popnow-0123456789abcdef"), "sk-popnow-…cdef")
        self.assertTrue(C.key_looks_valid("sk-popnow-" + "a" * 48))
        self.assertFalse(C.key_looks_valid("hello"))


def _runner(client, job, events, **kw):
    spec = models.get(job.model)

    def build(j, uploaded):
        media = recipes.Uploaded(**{k: v for k, v in uploaded.items()})
        return recipes.build_input(spec, j.prompt, j.options, media)

    return JobRunner(client, job, events.append, build_input=build, sleep=lambda s: time.sleep(0.01), **kw)


class JobTests(FakeServerCase):
    def make_job(self, prompt="A cozy reading nook", model="bytedance/seedream-5.0-pro/edit", workflow="render", media=None):
        return Job(workflow=workflow, model=model, label="t", prompt=prompt, user_prompt=prompt, options={"resolution": "1k"}, media=media if media is not None else [MediaFile(self.png(), "structure")], out_dir=os.path.join(self.tmp, "out"))

    def test_render_flow_with_confirm(self):
        events: list = []
        job = self.make_job()
        r = _runner(self.client, job, events)
        r.start()
        deadline = time.time() + 5
        while job.state != "awaiting_confirm" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(job.state, "awaiting_confirm")
        self.assertEqual(job.max_credits, 36)
        r.confirm()
        r.join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertEqual(len(job.outputs), 1)
        with open(job.outputs[0], "rb") as f:
            self.assertTrue(f.read(8).startswith(b"\x89PNG"))
        self.assertEqual(job.credits, 36)
        self.assertTrue(job.input["image_urls"][0].startswith("popnow://f/"))

    def test_auto_confirm_below_threshold(self):
        job = self.make_job()
        r = _runner(self.client, job, [], auto_confirm_below=50)
        r.start()
        r.join(10)
        self.assertEqual(job.state, "succeeded")

    def test_cancel_before_submit_charges_nothing(self):
        job = self.make_job()
        r = _runner(self.client, job, [])
        r.start()
        deadline = time.time() + 5
        while job.state != "awaiting_confirm" and time.time() < deadline:
            time.sleep(0.01)
        r.cancel()
        r.join(5)
        self.assertEqual(job.state, "canceled")
        self.assertIn("Nothing was charged", job.status)
        self.assertEqual(self.state.balance, 5000)
        self.assertFalse(any(p == "/api/v1/jobs/createTask" for _, p in self.state.calls))

    def test_failed_task_reports_refund(self):
        job = self.make_job(prompt="FAIL please")
        r = _runner(self.client, job, [], auto_confirm_below=1000)
        r.start()
        r.join(10)
        self.assertEqual(job.state, "failed")
        self.assertTrue(job.refunded)
        self.assertIn("credits were refunded", job.status)
        self.assertEqual(self.state.balance, 5000)

    def test_stop_after_submit_then_resume(self):
        job = self.make_job()
        r = JobRunner(self.client, job, lambda j: None, build_input=lambda j, u: {"prompt": "x", "image_urls": u["structure"]}, auto_confirm_below=1000, sleep=lambda s: time.sleep(0.2))
        r.start()
        deadline = time.time() + 5
        while not job.task_id and time.time() < deadline:
            time.sleep(0.01)
        r.cancel()
        r.join(5)
        self.assertEqual(job.state, "stopped")
        self.assertTrue(job.resumable)
        r2 = JobRunner(self.client, job, lambda j: None, sleep=lambda s: None)
        r2.start()
        r2.join(5)
        self.assertEqual(job.state, "succeeded")

    def test_video_flow(self):
        clip = os.path.join(self.tmp, "clay.mp4")
        with open(clip, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        job = self.make_job(model="bytedance/seedance-2.0-mini/reference-to-video", workflow="animate", media=[MediaFile(clip, "clip", 5.0)])
        job.options = {"duration": 5, "resolution": "480p"}
        r = _runner(self.client, job, [], auto_confirm_below=1000)
        r.start()
        r.join(10)
        self.assertEqual(job.state, "succeeded", job.status)
        self.assertTrue(job.outputs[0].endswith(".mp4"))
        self.assertEqual(job.input["reference_video_urls"][0][:11], "popnow://f/")
        self.assertEqual(job.input["duration_seconds"], 5)

    def test_history_roundtrip(self):
        job = self.make_job()
        job.state, job.task_id = "running", "tsk_x"
        p = os.path.join(self.tmp, "h", "history.json")
        history.save(p, [job])
        back = history.load(p)
        self.assertEqual(back[0].state, "stopped")  # a task that was running when closed comes back resumable
        self.assertEqual(back[0].task_id, "tsk_x")
        self.assertNotIn("input", open(p).read())


class ModelTests(unittest.TestCase):
    def test_every_workflow_has_models(self):
        for w in models.WORKFLOWS:
            self.assertTrue(models.models_for(w), w)

    def test_estimates_match_site_prices(self):
        # matches the credit prices shown on popnow.ai
        pro = models.get("bytedance/seedream-5.0-pro/edit")
        self.assertEqual(models.estimate(pro, {"resolution": "1k"}).credits, 36)
        self.assertEqual(models.estimate(pro, {"resolution": "2k"}).credits, 72)
        mini = models.get("bytedance/seedance-2.0-mini/reference-to-video")
        e = models.estimate(mini, {"resolution": "480p", "duration_seconds": 5, "reference_video_urls": ["popnow://f/x"]}, clip_seconds=[5.0])
        self.assertEqual(e.credits, 110)  # (5 + 5) × 10.97 = 109.7 → 110
        self.assertFalse(e.approx)
        wan = models.get("alibaba/wan-3.0/reference-to-video")
        self.assertEqual(models.estimate(wan, {"resolution": "720p", "duration_seconds": 5, "reference_video_urls": ["popnow://f/x"]}, clip_seconds=[5.0]).credits, 900)
        turbo = models.get("alibaba/qwen-image-2.1-turbo/edit")
        self.assertEqual(models.estimate(turbo, {}).credits, 15)
        kling = models.get("kling/o3/video-edit")
        self.assertEqual(models.estimate(kling, {"resolution": "720p"}, output_seconds=5).credits, 626)

    def test_duration_options_drop_smart(self):
        mini = models.get("bytedance/seedance-2.0-mini/reference-to-video")
        self.assertNotIn(-1, mini.option_values("duration"))
        self.assertEqual(mini.option_default("duration"), 5)

    def test_closest_aspect(self):
        vals = ["auto", "1:1", "16:9", "9:16", "4:3"]
        self.assertEqual(models.closest_aspect(vals, 1920, 1080), "16:9")
        self.assertEqual(models.closest_aspect(vals, 1000, 1010), "1:1")
        self.assertEqual(models.closest_aspect(vals, 1080, 1920), "9:16")

    def test_every_bundled_model_has_a_price(self):
        for w in models.WORKFLOWS:
            for spec in models.models_for(w):
                self.assertTrue(spec.label, spec.model)
                self.assertIsNotNone(models.estimate(spec, {}), spec.model)


class RecipeTests(unittest.TestCase):
    def test_render_input_places_structure_first(self):
        spec = models.get("bytedance/seedream-5.0-pro/edit")
        prompt = recipes.render_prompt(spec, "A red vintage car", "photoreal", pass_kind="depth", n_refs=1)
        self.assertIn("Image 1 is a grey clay render", prompt)
        self.assertIn("Image 2 is a depth map", prompt)
        self.assertIn("Use Image 3 as the reference", prompt)
        inp = recipes.build_input(spec, prompt, {"resolution": "1k", "aspect": "16:9"}, recipes.Uploaded(structure=["u1", "u2"], refs=["r1"]))
        self.assertEqual(inp["image_urls"], ["u1", "u2", "r1"])
        self.assertEqual(inp["aspect_ratio"], "16:9")

    def test_last_render_prompt_keeps_lighting(self):
        spec = models.get("bytedance/seedream-5.0-pro/edit")
        p = recipes.render_prompt(spec, "", "none", pass_kind="none", n_refs=0, source="render")
        self.assertIn("Image 1 is a 3D render", p)
        self.assertIn("lighting direction", p)
        self.assertNotIn("grey clay", p)

    def test_controlnet_uses_control_field(self):
        spec = models.get("black-forest-labs/flux-1-dev-controlnet-lora/text-to-image")
        inp = recipes.build_input(spec, recipes.render_prompt(spec, "a cabin", "none", pass_kind="depth", n_refs=0), {"control_strength": 0.6}, recipes.Uploaded(control=["c1"]))
        self.assertEqual(inp["control_image_url"], "c1")
        self.assertEqual(inp["control_strength"], 0.6)
        self.assertNotIn("grey clay", inp["prompt"])

    def test_animate_tokens_follow_model(self):
        sd = models.get("bytedance/seedance-2.0-mini/reference-to-video")
        p = recipes.animate_prompt(sd, "two friends walk through a park", "cinematic", n_refs=1)
        self.assertIn("@Video1 is a grey clay previz", p)
        self.assertIn("@Image1 shows", p)
        wan = models.get("alibaba/wan-3.0/reference-to-video")
        self.assertIn("Video 1 is a grey clay previz", recipes.animate_prompt(wan, "", "none", n_refs=0))
        kl = models.get("kling/o3/video-edit")
        self.assertIn("<<<image_1>>>", recipes.animate_prompt(kl, "x", "none", n_refs=1))
        anim = models.get("alibaba/wan-2.2-animate/character-animation")
        self.assertEqual(recipes.missing_inputs(anim, recipes.Uploaded(clip=["c"]), "")[0][:16], "Pick a character")
        inp = recipes.build_input(anim, "", {}, recipes.Uploaded(clip=["c"], character=["p"]))
        self.assertEqual(inp, {"video_url": "c", "image_url": "p"})

    def test_texture_prompt(self):
        p = recipes.texture_prompt("mossy cobblestone", "scan")
        self.assertTrue(p.startswith("Seamless tileable texture of mossy cobblestone."))


try:
    import numpy as np

    from popnow.core import imaging
except ImportError:  # the system Python has no numpy
    np = None


@unittest.skipIf(np is None, "numpy not installed")
class ImagingTests(unittest.TestCase):
    def test_make_tileable_removes_seams(self):
        rng = np.random.default_rng(1)
        h, w = 128, 160
        y, x = np.mgrid[0:h, 0:w]
        img = np.stack([x / w, y / h, (x + y) / (w + h)], axis=2).astype(np.float32) + rng.normal(0, 0.01, (h, w, 3)).astype(np.float32)
        self.assertGreater(imaging.seam_error(img), 0.5)
        out = imaging.make_tileable(img)
        self.assertEqual(out.shape, (h - 16, w - 20, 3))
        self.assertLess(imaging.seam_error(out), 0.05)
        # the middle part is kept as is
        np.testing.assert_allclose(out[40, 60], img[40, 60], atol=1e-6)

    def test_lineart_finds_edges(self):
        d = np.zeros((32, 32, 3), np.float32)
        d[8:24, 8:24] = 0.8
        lines = imaging.lineart(d, thicken=0)
        self.assertEqual(lines.shape, (32, 32, 3))
        self.assertEqual(lines[16, 8, 0], 1.0)
        self.assertEqual(lines[16, 16, 0], 0.0)

    def test_composite(self):
        rgba = np.zeros((2, 2, 4), np.float32)
        out = imaging.composite(rgba, (0.5, 0.5, 1.0))
        np.testing.assert_allclose(out[0, 0], [0.5, 0.5, 1.0])


if __name__ == "__main__":
    unittest.main()
