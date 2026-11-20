"""Smoke test without Blender: import and register the whole add-on with a fake bpy, run every panel's draw(),
then run the Texture / Render operator entry points once on a fake scene (render functions replaced with stubs).

It catches the add-on's own mistakes (misspelled property names, missing imports, panels referencing settings that do not exist, registration order),
not differences in the Blender API itself; those are covered by tests/blender_selftest.py running in real Blender.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)


# ---------------- fake bpy ----------------


class _Prop:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.kw = kw


def _prop_factory(kind):
    return lambda **kw: _Prop(kind, **kw)


class _Layout:
    """Records calls; every layout method returns itself, any attribute can be set"""

    def __init__(self, log):
        self.__dict__["log"] = log

    def __getattr__(self, name):
        def call(*a, **kw):
            self.log.append((name, a, kw))
            if name in ("prop", "template_ID") and len(a) >= 2:
                owner, attr = a[0], a[1]
                if not hasattr(owner, attr):
                    raise AttributeError(f"layout.{name}: {type(owner).__name__} has no property {attr!r}")
            if name == "operator":
                return types.SimpleNamespace()
            return self

        return call

    def __setattr__(self, k, v):
        self.__dict__[k] = v


class _Collection(list):
    def get(self, k, default=None):
        return default


def make_fake_bpy(tmp):
    bpy = types.ModuleType("bpy")
    t = types.ModuleType("bpy.types")

    class _Base:
        bl_rna = types.SimpleNamespace(properties={})

        def report(self, kind, msg):
            self.reported = (kind, msg)

    for name in ("Operator", "Panel", "AddonPreferences", "PropertyGroup", "Image", "Object", "Material", "MovieClip", "Scene", "Camera", "RenderSettings"):
        setattr(t, name, type(name, (_Base,), {}))
    bpy.types = t
    props = types.ModuleType("bpy.props")
    for k in ("StringProperty", "BoolProperty", "IntProperty", "FloatProperty", "EnumProperty", "PointerProperty", "CollectionProperty"):
        setattr(props, k, _prop_factory(k))
    bpy.props = props
    registered = []
    bpy.utils = types.SimpleNamespace(
        register_class=registered.append,
        unregister_class=lambda c: registered.remove(c),
        extension_path_user=lambda pkg, path="", create=False: os.path.join(tmp, "ext", path),
        user_resource=lambda *a, **k: os.path.join(tmp, "res"),
    )
    timers = []
    bpy.app = types.SimpleNamespace(
        version=(4, 2, 0),
        online_access=True,
        timers=types.SimpleNamespace(register=lambda f, **k: timers.append(f), unregister=lambda f: timers.remove(f) if f in timers else None, is_registered=lambda f: f in timers),
        handlers=types.SimpleNamespace(load_post=[], persistent=lambda f: f),
    )
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.data = types.SimpleNamespace(filepath="", scenes=[], images=[], objects={}, movieclips=None)
    bpy.ops = types.SimpleNamespace()
    bpy._registered = registered
    bpy._timers = timers
    return bpy


def build_scene_props(props_mod):
    """Build fake scene settings with defaults from the PropertyGroup annotations"""
    cls = props_mod.PopNowSceneProps
    ns = types.SimpleNamespace()
    for name, p in cls.__annotations__.items():
        if p.kind == "EnumProperty":
            items = p.kw["items"]
            if callable(items):
                items = items(ns, None) if name.endswith("_model") else []
            setattr(ns, name, p.kw.get("default") or (items[0][0] if items else "NONE"))
        elif p.kind == "PointerProperty":
            setattr(ns, name, None)
        else:
            setattr(ns, name, p.kw.get("default", "" if p.kind == "StringProperty" else 0))
    # Model-dependent dropdowns: fill from the model defaults (same code as the add-on's init_defaults)
    ns.defaults_set = False
    props_mod._reset_options("render", "render_model", ("aspect", "resolution", "quality"))(ns, None)
    props_mod._reset_options("anim", "anim_model", ("aspect", "resolution", "duration", "animation_mode"))(ns, None)
    props_mod._reset_options("tex", "tex_model", ("resolution",))(ns, None)
    return ns


class BpySmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="popnow-bpy-")
        cls.bpy = make_fake_bpy(cls.tmp)
        sys.modules["bpy"] = cls.bpy
        sys.modules["bpy.types"] = cls.bpy.types
        sys.modules["bpy.props"] = cls.bpy.props
        mathutils = types.ModuleType("mathutils")
        mathutils.Vector = tuple
        sys.modules["mathutils"] = mathutils
        for m in [k for k in sys.modules if k == "popnow" or k.startswith("popnow.ui") or k == "popnow.__init__"]:
            del sys.modules[m]
        cls.addon = importlib.import_module("popnow")
        from popnow.ui import ops, panels, props, runtime

        cls.ops, cls.panels, cls.props_mod, cls.runtime = ops, panels, props, runtime

    @classmethod
    def tearDownClass(cls):
        for k in ("bpy", "bpy.types", "bpy.props", "mathutils"):
            sys.modules.pop(k, None)

    def context(self, selected=(), api_key="sk-popnow-" + "a" * 48):
        scene = types.SimpleNamespace(popnow=build_scene_props(self.props_mod), camera=object(), frame_start=1, render=types.SimpleNamespace(fps=24, fps_base=1.0, resolution_x=1920, resolution_y=1080))
        prefs = types.SimpleNamespace(api_key=api_key, base_url="http://127.0.0.1:9/api/v1", mcp_url="http://127.0.0.1:9/mcp", output_dir=os.path.join(self.tmp, "out"), auto_confirm_usd=0.0, auto_confirm_below=0, auto_confirm_credits=0, open_results=False)
        addons = {self.runtime.ADDON_ID: types.SimpleNamespace(preferences=prefs)}
        ctx = types.SimpleNamespace(scene=scene, selected_objects=list(selected), preferences=types.SimpleNamespace(addons=addons), window_manager=types.SimpleNamespace(windows=[], progress_begin=lambda *a: None, progress_update=lambda *a: None, progress_end=lambda: None))
        self.bpy.context = ctx
        return ctx

    def test_registers_all_classes(self):
        self.addon.register()
        names = {c.__name__ for c in self.bpy._registered}
        self.assertIn("POPNOW_AP_preferences", names)
        self.assertIn("POPNOW_OT_render", names)
        self.assertIn("POPNOW_PT_tasks_image", names)
        self.assertEqual(self.runtime.ADDON_ID, "popnow")
        # the preferences bl_idname must equal the package name, otherwise Blender cannot find the preferences
        self.assertEqual(self.addon.prefs.POPNOW_AP_preferences.bl_idname, "popnow")

    def test_property_annotations_are_real_properties(self):
        # with `from __future__ import annotations` in a ui module, annotations become strings and Blender fails to register properties
        for cls in (self.props_mod.PopNowSceneProps, self.addon.prefs.POPNOW_AP_preferences, self.ops.POPNOW_OT_job_confirm):
            for name, ann in cls.__annotations__.items():
                self.assertNotIsInstance(ann, str, f"{cls.__name__}.{name}")
        ui_dir = os.path.join(HERE, "..", "popnow", "ui")
        for f in os.listdir(ui_dir):
            if f.endswith(".py"):
                with open(os.path.join(ui_dir, f), encoding="utf-8") as fh:
                    self.assertNotIn("from __future__ import annotations", fh.read(), f)

    def test_panels_draw_without_errors(self):
        ctx = self.context()
        self.runtime.BALANCE.update({"usd": "12.345", "held_usd": "0.006", "at": time.time()})
        from popnow.core.jobs import Job

        # one task in each state, covering every branch of the Tasks panel
        # (the last two entries look like task history written by 0.1.x, with credits only)
        for state, extra in [("awaiting_confirm", {"max_charge_usd": "0.006", "expected_charge_usd": "0.005"}), ("running", {"task_id": "tsk_1", "progress": 0.4}), ("succeeded", {"task_id": "tsk_2", "outputs": ["/x.png"], "cost_usd": "0.006", "settled": True}), ("failed", {"task_id": "tsk_3", "error_code": "content_rejected", "settled": True, "cost_usd": "0"}), ("stopped", {"task_id": "tsk_4"}), ("awaiting_confirm", {"max_credits": 6, "expected_credits": 5}), ("succeeded", {"task_id": "tsk_5", "outputs": ["/y.png"], "credits": 6, "settled": True})]:
            for wf in ("render", "animate", "texture"):
                j = Job(workflow=wf, model="m", label="L", prompt="p", user_prompt="a prompt", options={}, media=[], out_dir=self.tmp)
                j.state = state
                for k, v in extra.items():
                    setattr(j, k, v)
                if wf == "render" and state == "succeeded":
                    j.extra["camera"] = "Camera"
                self.runtime.JOBS[j.id] = j
        for cls in self.panels.CLASSES:
            for workflow_model in (None, "black-forest-labs/flux-1-dev-controlnet-lora/text-to-image", "alibaba/wan-2.2-animate/character-animation", "kling/o3/video-edit"):
                if workflow_model:
                    from popnow.core import models

                    wf = models.get(workflow_model).workflow
                    attr = {"render": "render_model", "animate": "anim_model"}[wf]
                    setattr(ctx.scene.popnow, attr, workflow_model)
                    self.props_mod._reset_options(attr.split("_")[0], attr, ("aspect", "resolution", "quality", "duration", "animation_mode"))(ctx.scene.popnow, None)
                panel = cls()
                log = []
                panel.layout = _Layout(log)
                panel.draw(ctx)
                if hasattr(panel, "draw_header"):
                    panel.draw_header(ctx)
                self.assertTrue(log, cls.__name__)
        self.runtime.JOBS.clear()

    def test_estimates_from_panel_settings(self):
        ctx = self.context()
        p = ctx.scene.popnow
        for wf in ("render", "animate", "texture"):
            est = self.ops.estimate_for(wf, p)
            self.assertIsNotNone(est, wf)
            self.assertGreater(est.credits, 0)
        # defaults: Seedream 5.0 Pro 2K = $0.072; Seedance 2.0 Mini default 720p 5 s + 5 s clay clip = $0.2359 (236 credits rounded up)
        from decimal import Decimal

        self.assertEqual(self.ops.estimate_for("render", p).usd, Decimal("0.072"))
        self.assertEqual(self.ops.estimate_for("animate", p).usd, Decimal("0.23588"))
        self.assertEqual(self.ops.estimate_for("render", p).credits, 72)
        self.assertEqual(self.ops.estimate_for("animate", p).credits, 236)

    def test_auto_confirm_threshold_moves_from_credits_to_usd(self):
        from decimal import Decimal

        ctx = self.context()
        p = ctx.preferences.addons[self.runtime.ADDON_ID].preferences
        self.assertEqual(self.runtime.auto_confirm_usd(), Decimal(0))
        p.auto_confirm_usd = 0.25
        self.assertEqual(self.runtime.auto_confirm_usd(), Decimal("0.25"))
        # a 0.1.x preference of 500 credits becomes $0.50 once, and the old value is cleared
        p.auto_confirm_usd, p.auto_confirm_below = 0.0, 500
        self.assertEqual(self.runtime.auto_confirm_usd(), Decimal("0.50"))
        self.assertEqual((p.auto_confirm_usd, p.auto_confirm_below), (0.5, 0))

    def test_texture_operator_builds_job(self):
        ctx = self.context(selected=[types.SimpleNamespace(name="Cube", data=types.SimpleNamespace(materials=[]))])
        ctx.scene.popnow.tex_prompt = "mossy cobblestone"
        started = []
        orig = self.runtime.start
        self.runtime.start = lambda job, build_input=None: started.append((job, build_input))
        try:
            op = self.ops.POPNOW_OT_texture()
            self.assertEqual(op.execute(ctx), {"FINISHED"})
        finally:
            self.runtime.start = orig
        job, build = started[0]
        self.assertEqual(job.workflow, "texture")
        self.assertEqual(job.extra["targets"], ["Cube"])
        self.assertIn("Seamless tileable texture of mossy cobblestone", job.prompt)
        inp = build(job, {})
        self.assertEqual(inp.get("aspect_ratio"), "1:1")

    def test_render_operator_with_stubbed_capture(self):
        ctx = self.context()
        p = ctx.scene.popnow
        p.render_pass = "depth"
        from contextlib import contextmanager

        cap = self.ops.capture
        saved = (cap.capture_camera, cap.render_clay_still, cap.structure_image, cap.last_render)

        @contextmanager
        def fake_cam(context, source):
            yield types.SimpleNamespace(name="Camera"), (1920, 1080)

        cap.capture_camera = fake_cam
        cap.render_clay_still = lambda *a, **k: os.path.join(self.tmp, "clay.png")
        cap.structure_image = lambda *a, **k: os.path.join(self.tmp, "depth.png")
        cap.last_render = lambda path: path
        ctx.scene.camera = types.SimpleNamespace(name="Camera")
        started = []
        orig = self.runtime.start
        self.runtime.start = lambda job, build_input=None: started.append((job, build_input))
        try:
            self.assertEqual(self.ops.POPNOW_OT_render().execute(ctx), {"FINISHED"})
            p.render_source = "RENDER"
            self.assertEqual(self.ops.POPNOW_OT_render().execute(ctx), {"FINISHED"})
        finally:
            self.runtime.start = orig
            cap.capture_camera, cap.render_clay_still, cap.structure_image, cap.last_render = saved
        self.assertTrue(started[1][0].media[0].path.endswith("render.png"))
        self.assertIn("is a 3D render", started[1][0].prompt)
        job, build = started[0]
        self.assertEqual([m.slot for m in job.media], ["structure", "structure"])
        self.assertEqual(job.options.get("aspect"), "16:9")
        inp = build(job, {"structure": ["popnow://f/a", "popnow://f/b"]})
        self.assertEqual(inp["image_urls"], ["popnow://f/a", "popnow://f/b"])
        self.assertIn("Image 2 is a depth map", inp["prompt"])

    # ---------------- account mode (signed in, no API key) ----------------

    def account_context(self, selected=()):
        from popnow.core import account

        os.environ.pop("POPNOW_API_KEY", None)
        ctx = self.context(selected, api_key="")
        self.runtime.account_store().set(account.Session("http://127.0.0.1:9", "http://127.0.0.1:9/mcp", "http://127.0.0.1:9/oauth/token", "pnc_1", "at-1", "rt-1"))
        return ctx

    def tearDown(self):
        self.runtime.account_store().clear()
        self.runtime.BALANCE.clear()
        self.runtime.LOGIN.clear()
        self.runtime.JOBS.clear()

    @staticmethod
    def _texts(log) -> list[str]:
        out = []
        for name, a, kw in log:
            if name == "label":
                out.append(str(kw.get("text") or (a[0] if a else "")))
            elif "text" in kw:
                out.append(str(kw["text"]))
        return out

    def test_mode_follows_key_then_account(self):
        self.context(api_key="")
        self.assertEqual(self.runtime.mode(), "")
        self.account_context()
        self.assertEqual(self.runtime.mode(), "account")
        self.assertEqual(self.runtime.account_path(), os.path.join(self.tmp, "ext", "account.json"))
        self.bpy.context.preferences.addons[self.runtime.ADDON_ID].preferences.api_key = "sk-popnow-" + "b" * 40
        self.assertEqual(self.runtime.mode(), "api")  # a key wins
        self.runtime.sign_out()
        self.assertFalse(os.path.exists(self.runtime.account_path()))

    def test_account_panels_show_credits_and_no_dollars(self):
        ctx = self.account_context()
        self.runtime.BALANCE.update({"mode": "account", "credits": 12000, "held_credits": 48, "at": time.time()})
        from popnow.core.jobs import Job

        for state, extra in [("awaiting_confirm", {"max_credits": 48}), ("awaiting_confirm", {"max_credits": 48, "extra": {"needs_consent": True}}), ("running", {"task_id": "job_1", "progress": 0.4}), ("succeeded", {"task_id": "job_2", "outputs": ["/x.png"], "credits": 48, "settled": True}), ("failed", {"task_id": "job_3", "credits": 0, "settled": True}), ("stopped", {"task_id": "job_4"})]:
            for wf in ("render", "animate", "texture"):
                j = Job(workflow=wf, model="", label="Auto", prompt="p", user_prompt="a prompt", options={}, media=[], out_dir=self.tmp, mode="account")
                j.state = state
                for k, v in extra.items():
                    setattr(j, k, dict(v) if isinstance(v, dict) else v)
                self.runtime.JOBS[j.id] = j
        ctx.scene.popnow.show_all_history = True
        texts: list[str] = []
        for character in (None, types.SimpleNamespace(name="Hero")):
            ctx.scene.popnow.anim_character = character
            for cls in self.panels.CLASSES:
                panel = cls()
                log = []
                panel.layout = _Layout(log)
                panel.draw(ctx)
                if hasattr(panel, "draw_header"):
                    panel.draw_header(ctx)
                texts += self._texts(log)
                # no model picker in account mode
                for name, a, kw in log:
                    if name == "prop" and len(a) >= 2:
                        self.assertNotIn(a[1], ("render_model", "anim_model", "tex_model", "render_resolution", "tex_resolution", "api_key"), cls.__name__)
        prefs_panel = self.addon.prefs.POPNOW_AP_preferences()
        for k, v in vars(ctx.preferences.addons[self.runtime.ADDON_ID].preferences).items():
            setattr(prefs_panel, k, v)
        log = []
        prefs_panel.layout = _Layout(log)
        prefs_panel.draw(ctx)
        prefs_texts = self._texts(log)
        joined = "\n".join(texts + prefs_texts)
        self.assertNotIn("$", joined)
        self.assertNotIn("USD", joined)
        self.assertIn("12,000 credits · 48 held", joined)
        self.assertIn("Start · 48 credits", joined)
        self.assertIn("48 credits", joined)
        self.assertTrue(any(t.startswith("Render with AI · about ") and t.endswith(" credits") for t in texts), texts)
        self.assertTrue(any(t.startswith("Generate Texture · about ") for t in texts))
        self.assertIn("Animate with AI", texts)  # with a character (copy_motion) the price comes with the quote
        self.assertIn("Signed in to PopNow", prefs_texts)
        ops_called = [a[0] for name, a, kw in log if name == "operator"]
        self.assertIn("popnow.sign_out", ops_called)

    def test_signed_out_panel_offers_sign_in(self):
        ctx = self.context(api_key="")
        panel = self.panels.POPNOW_PT_account()
        log = []
        panel.layout = _Layout(log)
        panel.draw(ctx)
        self.assertIn("popnow.sign_in", [a[0] for name, a, kw in log if name == "operator"])
        self.assertNotIn("$", "\n".join(self._texts(log)))
        self.runtime.LOGIN.update({"state": "waiting", "url": "https://example.invalid/authorize", "attempt": 1})
        log.clear()
        panel.draw(ctx)
        self.assertIn("popnow.copy_sign_in_link", [a[0] for name, a, kw in log if name == "operator"])

    def test_account_estimates(self):
        ctx = self.account_context()
        p = ctx.scene.popnow
        for wf in ("render", "animate", "texture"):
            n = self.ops.account_estimate(wf, p)
            self.assertIsInstance(n, int, wf)
            self.assertGreater(n, 0)
        p.anim_character = types.SimpleNamespace(name="Hero")
        self.assertIsNone(self.ops.account_estimate("animate", p))
        self.assertEqual(self.ops.account_aspect("MATCH", (1920, 1080)), "16:9")
        self.assertEqual(self.ops.account_aspect("2:1", (1, 1)), "16:9")
        self.assertEqual(self.ops.account_aspect("4:5", (1920, 1080)), "4:5")

    def test_account_texture_and_render_jobs(self):
        ctx = self.account_context(selected=[types.SimpleNamespace(name="Cube", data=types.SimpleNamespace(materials=[]))])
        p = ctx.scene.popnow
        p.tex_prompt = "mossy cobblestone"
        p.render_pass = "depth"
        p.render_prompt = "a cabin"
        from contextlib import contextmanager
        from popnow.core.account_jobs import tool_call

        cap = self.ops.capture
        saved = (cap.capture_camera, cap.render_clay_still, cap.structure_image)

        @contextmanager
        def fake_cam(context, source):
            yield types.SimpleNamespace(name="Camera"), (1920, 1080)

        cap.capture_camera = fake_cam
        cap.render_clay_still = lambda *a, **k: os.path.join(self.tmp, "clay.png")
        cap.structure_image = lambda *a, **k: os.path.join(self.tmp, "depth.png")
        ctx.scene.camera = types.SimpleNamespace(name="Camera")
        started = []
        orig = self.runtime.start
        self.runtime.start = lambda job, build_input=None: started.append(job)
        try:
            self.assertEqual(self.ops.POPNOW_OT_texture().execute(ctx), {"FINISHED"})
            self.assertEqual(self.ops.POPNOW_OT_render().execute(ctx), {"FINISHED"})
        finally:
            self.runtime.start = orig
            cap.capture_camera, cap.render_clay_still, cap.structure_image = saved
        tex, render = started
        self.assertEqual((tex.mode, tex.model, tex.label), ("account", "", "Auto"))
        self.assertEqual(tool_call(tex, {}), ("create_image", {"prompt": tex.prompt, "aspect_ratio": "1:1"}))
        self.assertEqual(render.mode, "account")
        self.assertEqual([m.slot for m in render.media], ["structure", "structure"])
        tool, args = tool_call(render, {"structure": ["popnow://f/a", "popnow://f/b"]})
        self.assertEqual(tool, "edit_image")
        self.assertEqual((args["image_url"], args["more_image_urls"], args["aspect_ratio"]), ("popnow://f/a", ["popnow://f/b"], "16:9"))
        self.assertTrue(args["prompt"][0].isupper())
        self.assertIn("Image 2 is a depth map", args["prompt"].replace("image 2", "Image 2"))
        self.assertNotIn("@", args["prompt"])
        self.assertIsInstance(render.estimate, int)


if __name__ == "__main__":
    unittest.main()
