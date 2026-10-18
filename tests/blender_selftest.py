"""Self-test that runs in real Blender: register the add-on -> build a small scene -> render clay image / structure passes / clay video -> run the full flow against the fake backend (or the real API).

Usage (from the repository root):
    blender --background --factory-startup --python tests/blender_selftest.py            # fake backend, no cost
  POPNOW_API_KEY=sk-popnow-… blender --background --factory-startup --python tests/blender_selftest.py -- --live
            # real API: runs only one cheapest Render (Qwen-Image 2.1 Turbo, about 3 credits)

Background mode has no event loop, so bpy.app.timers never fire on their own; runtime._tick() is called by hand here. A non-zero exit code means failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

LIVE = "--live" in sys.argv
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def enable_addon():
    import addon_utils

    mod = addon_utils.enable("popnow", default_set=True, handle_error=lambda e: traceback.print_exc())
    check("addon registers", mod is not None)
    return mod


def build_scene() -> None:
    scene = bpy.context.scene
    for ob in list(scene.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_plane_add(size=10)
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1))
    cube = bpy.context.active_object
    cube.color = (0.9, 0.2, 0.2, 1)
    bpy.ops.mesh.primitive_uv_sphere_add(location=(2.2, 0.5, 0.8), radius=0.8)
    bpy.ops.object.camera_add(location=(7, -7, 4.5), rotation=(1.15, 0, 0.785))
    scene.camera = bpy.context.active_object
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    scene.frame_start, scene.frame_end = 1, 48
    scene.render.fps = 24
    # a simple camera move so the clay video has some motion
    cam = scene.camera
    cam.keyframe_insert("location", frame=1)
    cam.location.x -= 2
    cam.keyframe_insert("location", frame=120)
    bpy.ops.object.select_all(action="DESELECT")
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube


def pump(runtime, job, timeout: float = 60.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        runtime._tick()
        if job.state == "awaiting_confirm":
            runtime.confirm(job.id)
        if not job.is_active and job.state != "awaiting_confirm":
            runtime._tick()
            return
        time.sleep(0.1)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="popnow-selftest-")
    srv = None
    if not LIVE:
        import fake_server

        srv, _state = fake_server.start(0)
        os.environ["POPNOW_API_KEY"] = fake_server.VALID_KEY_PREFIX + "selftest0000000000000000"
        os.environ["POPNOW_API_BASE"] = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
    mod = enable_addon()
    if mod is None:
        return
    from popnow.ui import capture, results, runtime  # noqa: E402
    from popnow.core import imaging  # noqa: E402

    p = runtime.prefs()
    p.output_dir = os.path.join(tmp, "out")
    p.base_url = os.environ.get("POPNOW_API_BASE", p.base_url)
    p.open_results = False
    p.auto_confirm_below = 0
    build_scene()
    from popnow.ui import props as props_mod

    props_mod.init_defaults(bpy.context.scene)
    sp = bpy.context.scene.popnow
    check("defaults applied", sp.defaults_set and sp.render_resolution != "NONE")

    ctx = bpy.context
    with capture.capture_camera(ctx, "CAMERA") as (cam, size):
        clay = capture.render_clay_still(ctx, cam, size, "CLAY", os.path.join(tmp, "clay"))
        check("clay still rendered", os.path.getsize(clay) > 1000)
        for kind in ("depth", "normal", "lineart"):
            try:
                path = capture.structure_image(ctx, cam, size, kind, tmp)
                arr = capture.load_pixels(path)
                check(f"{kind} pass rendered", arr.shape[0] > 100 and float(arr[..., :3].std()) > 0.01, f"std {arr[..., :3].std():.4f}")
            except Exception as e:
                check(f"{kind} pass rendered", False, repr(e))
        try:
            clip, secs = capture.render_clay_clip(ctx, cam, size, "OBJECT", 5, tmp)
            check("clay clip rendered", os.path.getsize(clip) > 1000 and abs(secs - 5) < 0.1, clip)
        except Exception as e:
            check("clay clip rendered", False, repr(e))
    check("scene settings restored", bpy.context.scene.render.resolution_x == 1280 and bpy.context.scene.render.engine != "CYCLES")

    # framing from the viewport has no viewport in background mode: it should fall back to the camera
    with capture.capture_camera(ctx, "VIEWPORT") as (cam2, _):
        check("viewport falls back to camera in background", cam2 == bpy.context.scene.camera)

    if LIVE:
        sp.render_model = "alibaba/qwen-image-2.1-turbo/edit"
        sp.render_prompt = "A small red toy block and a white ball on a wooden table, soft daylight"
        sp.render_resolution = "1k"
    r = bpy.ops.popnow.render()
    check("render operator accepted", r == {"FINISHED"})
    job = runtime.jobs_sorted()[0]
    pump(runtime, job, 240 if LIVE else 30)
    check("render job succeeded", job.state == "succeeded", job.status)
    check("render result loaded", any(img.filepath and job.task_id[-10:] in img.filepath for img in bpy.data.images), str(job.outputs))
    if LIVE:
        print(f"live render: task {job.task_id}, {job.credits} credits, file {job.outputs}")
        finish(srv, tmp)
        return

    bpy.ops.popnow.result_camera_bg(job_id=job.id)
    check("camera background set", len(bpy.context.scene.camera.data.background_images) == 1)
    bpy.ops.popnow.result_project(job_id=job.id)
    check("projection modifier added", "PopNow Projection" in bpy.context.active_object.modifiers)

    sp.anim_model = "bytedance/seedance-2.0-mini/reference-to-video"
    sp.anim_prompt = "A red cube rolls across a wooden table"
    r = bpy.ops.popnow.animate()
    job = runtime.jobs_sorted()[0]
    pump(runtime, job)
    check("animate job succeeded", job.state == "succeeded", job.status)
    bpy.ops.popnow.result_vse(job_id=job.id)
    se = bpy.context.scene.sequence_editor
    strips = se.strips_all if hasattr(se, "strips_all") else se.sequences_all
    check("video added to sequencer", len(strips) == 1)

    sp.tex_prompt = "mossy cobblestone"
    r = bpy.ops.popnow.texture()
    job = runtime.jobs_sorted()[0]
    pump(runtime, job)
    check("texture job succeeded", job.state == "succeeded", job.status)
    check("texture is seamless", (job.extra.get("seam_error") or 1) < 0.2, str(job.extra))
    check("texture applied to selection", bpy.context.active_object.active_material is not None and bpy.context.active_object.active_material.name.startswith("PopNow"))

    sp.render_prompt = "FAIL on purpose"
    bpy.ops.popnow.render()
    job = runtime.jobs_sorted()[0]
    pump(runtime, job)
    check("failure reports refund", job.state == "failed" and "refunded" in job.status, job.status)
    _ = (results, imaging)
    finish(srv, tmp)


def finish(srv, tmp) -> None:
    import addon_utils

    addon_utils.disable("popnow", default_set=True)
    check("addon unregisters", not hasattr(bpy.types.Scene, "popnow"))
    if srv is not None:
        srv.shutdown()
    print(f"selftest temp dir: {tmp}")


try:
    main()
except Exception:
    traceback.print_exc()
    failures.append("exception")
print(f"\n{len(failures)} failure(s)" if failures else "\nALL PASS")
sys.exit(1 if failures else 0)
