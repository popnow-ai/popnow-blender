"""Operators: the Render with AI / Animate with AI / Texture entry points, confirming, canceling and resuming tasks, and where results go."""

import os
import shutil
import uuid

import bpy
from bpy.props import IntProperty, StringProperty

from . import capture, results, runtime
from .props import option_value
from ..core import models, recipes
from ..core.client import CONSOLE_URL, PRICING_URL
from ..core.jobs import Job, MediaFile

MAX_INPUT_DIRS = 30


def _ready(op, context) -> bool:
    """Whether requests may be sent: online access allowed and a key present"""
    if not runtime.online_allowed():
        op.report({"ERROR"}, "Turn on Allow Online Access in Preferences > System to use PopNow.")
        return False
    status = runtime.key_status()
    if status == "missing":
        op.report({"ERROR"}, "Add your PopNow API key first (PopNow panel, or Preferences > Add-ons > PopNow).")
        return False
    if status == "malformed":
        op.report({"ERROR"}, "The API key does not look right. Copy it again from the PopNow developer console.")
        return False
    return True


def _input_dir(job_id: str) -> str:
    """One input folder per task; only the latest MAX_INPUT_DIRS are kept"""
    root = runtime.user_dir("inputs")
    dirs = sorted((os.path.join(root, d) for d in os.listdir(root)), key=os.path.getmtime)
    for old in dirs[:-MAX_INPUT_DIRS] if len(dirs) >= MAX_INPUT_DIRS else []:
        shutil.rmtree(old, ignore_errors=True)
    path = os.path.join(root, job_id)
    os.makedirs(path, exist_ok=True)
    return path


def _build_input(job: Job, uploaded: dict) -> dict:
    spec = models.get(job.model)
    return recipes.build_input(spec, job.prompt, job.options, recipes.Uploaded(**{k: list(v) for k, v in uploaded.items()}))


def _aspect(spec, ident: str, size) -> str | None:
    if not spec.option("aspect"):
        return None
    if ident == "MATCH":
        return models.closest_aspect(spec.option_values("aspect"), *size)
    return option_value(spec, "aspect", ident)


def _refs(images, spec, job_dir: str) -> list[MediaFile]:
    out = []
    for i, img in enumerate([im for im in images if im is not None][: spec.max_refs]):
        out.append(MediaFile(capture.export_image(img, os.path.join(job_dir, f"ref{i + 1}")), "refs"))
    return out


def render_options(spec, props, size) -> dict:
    opts = {
        "aspect": _aspect(spec, props.render_aspect, size),
        "resolution": option_value(spec, "resolution", props.render_resolution),
        "quality": option_value(spec, "quality", props.render_quality),
    }
    if spec.option("control_strength"):
        opts["control_strength"] = round(float(props.control_strength), 2)
    return {k: v for k, v in opts.items() if v is not None}


def animate_seconds(spec, props) -> float:
    if spec.uses_duration_option:
        d = option_value(spec, "duration", props.anim_duration) or spec.option_default("duration") or 5
    else:
        d = props.anim_clip_seconds
    return capture.clip_seconds_for(spec, float(d))


def animate_options(spec, props, size) -> dict:
    opts = {
        "aspect": _aspect(spec, props.anim_aspect, size),
        "resolution": option_value(spec, "resolution", props.anim_resolution),
        "duration": option_value(spec, "duration", props.anim_duration) if spec.uses_duration_option else None,
        "audio": bool(props.anim_audio) if spec.option("audio") else None,
        "animation_mode": option_value(spec, "animation_mode", props.anim_animation_mode),
    }
    if opts["duration"] is not None:
        # Output length must match the clay clip (the clay clip is bound by the model's reference video limit)
        opts["duration"] = int(animate_seconds(spec, props))
        vals = spec.option_values("duration")
        if opts["duration"] not in vals:
            opts["duration"] = min(vals, key=lambda v: abs(v - opts["duration"]))
    return {k: v for k, v in opts.items() if v is not None}


def estimate_for(workflow: str, props, size=(16, 9)) -> models.Estimate | None:
    """Live estimate shown in the panel as settings change (ignores changes from the number of reference images; the quote is authoritative)"""
    if workflow == "render":
        spec = models.get(props.render_model)
        if not spec:
            return None
        opts = render_options(spec, props, size)
        return models.estimate(spec, {spec.option(k)["field"]: v for k, v in opts.items()})
    if workflow == "animate":
        spec = models.get(props.anim_model)
        if not spec:
            return None
        secs = animate_seconds(spec, props)
        opts = animate_options(spec, props, size)
        inp = {spec.option(k)["field"]: v for k, v in opts.items()}
        clip = spec.media("clip")
        inp[clip["field"]] = ["clip"] if clip["array"] else "clip"
        return models.estimate(spec, inp, clip_seconds=[secs], output_seconds=secs)
    spec = models.get(props.tex_model)
    if not spec:
        return None
    opts = {"resolution": option_value(spec, "resolution", props.tex_resolution)}
    return models.estimate(spec, {spec.option(k)["field"]: v for k, v in opts.items() if v is not None and spec.option(k)})


class _Base(bpy.types.Operator):
    bl_options = {"REGISTER"}

    def _start(self, job: Job, spec) -> None:
        job.extra["typical_seconds"] = spec.typical_seconds
        runtime.start(job, build_input=_build_input)
        self.report({"INFO"}, "Sent to PopNow. Watch the Tasks panel.")


class POPNOW_OT_render(_Base):
    bl_idname = "popnow.render"
    bl_label = "Render with AI"
    bl_description = "Render the current view as a clay image and turn it into a finished picture with PopNow"

    def execute(self, context):
        if not _ready(self, context):
            return {"CANCELLED"}
        props = context.scene.popnow
        spec = models.get(props.render_model)
        if spec is None:
            self.report({"ERROR"}, "Pick a model.")
            return {"CANCELLED"}
        job_id = uuid.uuid4().hex[:10]
        job_dir = _input_dir(job_id)
        wm = context.window_manager
        wm.progress_begin(0, 3)
        try:
            source = props.render_source
            with capture.capture_camera(context, "CAMERA" if source == "RENDER" else source) as (cam, size):
                if source == "RENDER":
                    # "Last F12 render": use the user's own lit and textured render as the base (same idea as AI Render running after a render)
                    clay = capture.last_render(os.path.join(job_dir, "render.png"))
                else:
                    clay = capture.render_clay_still(context, cam, size, props.render_look, os.path.join(job_dir, "clay"))
                wm.progress_update(1)
                media: list[MediaFile] = []
                pass_kind = props.render_pass
                if spec.media("control"):
                    ctrl = capture.structure_image(context, cam, size, pass_kind if pass_kind != "none" else "depth", job_dir)
                    media.append(MediaFile(ctrl, "control"))
                    pass_kind = pass_kind if pass_kind != "none" else "depth"
                else:
                    media.append(MediaFile(clay, "structure"))
                    if pass_kind != "none":
                        media.append(MediaFile(capture.structure_image(context, cam, size, pass_kind, job_dir), "structure"))
                wm.progress_update(2)
                camera_name = cam.name if source in ("CAMERA", "RENDER") or cam == context.scene.camera else ""
                aspect = (context.scene.render.resolution_x, context.scene.render.resolution_y) if camera_name else size
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            wm.progress_end()
        refs = [] if spec.media("control") else _refs([props.ref_1, props.ref_2, props.ref_3], spec, job_dir)
        media += refs
        prompt = recipes.render_prompt(spec, props.render_prompt, props.render_style, pass_kind=pass_kind if not spec.media("control") else "none", n_refs=len(refs), source="render" if props.render_source == "RENDER" else "clay")
        job = Job(id=job_id, workflow="render", model=spec.model, label=spec.label, prompt=prompt, user_prompt=props.render_prompt, options=render_options(spec, props, size), media=media, out_dir=runtime.output_dir())
        est = estimate_for("render", props, size)
        job.estimate = est.credits if est else None
        job.extra.update({"camera": camera_name, "aspect": list(aspect), "clay": clay})
        self._start(job, spec)
        return {"FINISHED"}


class POPNOW_OT_animate(_Base):
    bl_idname = "popnow.animate"
    bl_label = "Animate with AI"
    bl_description = "Render your camera animation as a short clay preview and turn it into a finished video with PopNow"

    def execute(self, context):
        if not _ready(self, context):
            return {"CANCELLED"}
        props = context.scene.popnow
        spec = models.get(props.anim_model)
        if spec is None:
            self.report({"ERROR"}, "Pick a model.")
            return {"CANCELLED"}
        if spec.media("character") and props.anim_character is None:
            self.report({"ERROR"}, "This model needs a Character image: the person who should perform the motion.")
            return {"CANCELLED"}
        secs = animate_seconds(spec, props)
        job_id = uuid.uuid4().hex[:10]
        job_dir = _input_dir(job_id)
        try:
            with capture.capture_camera(context, props.source) as (cam, size):
                rules = spec.clip_rules
                clip, real = capture.render_clay_clip(context, cam, size, props.anim_look, secs, job_dir, short_side=int(rules.get("minSide") or 720))
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        media = [MediaFile(clip, "clip", real)]
        refs = _refs([props.anim_ref_1, props.anim_ref_2], spec, job_dir) if spec.media("refs") else []
        media += refs
        if spec.media("character"):
            media.append(MediaFile(capture.export_image(props.anim_character, os.path.join(job_dir, "character")), "character"))
        prompt = recipes.animate_prompt(spec, props.anim_prompt, props.anim_style, n_refs=len(refs))
        job = Job(id=job_id, workflow="animate", model=spec.model, label=spec.label, prompt=prompt, user_prompt=props.anim_prompt, options=animate_options(spec, props, size), media=media, out_dir=runtime.output_dir())
        est = estimate_for("animate", props, size)
        job.estimate = est.credits if est else None
        job.extra.update({"clip": clip, "seconds": real, "camera": context.scene.camera.name if context.scene.camera else ""})
        self._start(job, spec)
        return {"FINISHED"}


class POPNOW_OT_texture(_Base):
    bl_idname = "popnow.texture"
    bl_label = "Generate Texture"
    bl_description = "Generate a tileable texture from your prompt and (optionally) put it on the selected objects"

    def execute(self, context):
        if not _ready(self, context):
            return {"CANCELLED"}
        props = context.scene.popnow
        spec = models.get(props.tex_model)
        if spec is None:
            self.report({"ERROR"}, "Pick a model.")
            return {"CANCELLED"}
        if not props.tex_prompt.strip():
            self.report({"ERROR"}, "Describe the material first, for example: mossy cobblestone.")
            return {"CANCELLED"}
        opts = {"resolution": option_value(spec, "resolution", props.tex_resolution)}
        if "1:1" in spec.option_values("aspect"):
            opts["aspect"] = "1:1"
        opts = {k: v for k, v in opts.items() if v is not None}
        targets = [o.name for o in context.selected_objects if hasattr(getattr(o, "data", None), "materials")] if props.tex_apply else []
        job = Job(workflow="texture", model=spec.model, label=spec.label, prompt=recipes.texture_prompt(props.tex_prompt, props.tex_style), user_prompt=props.tex_prompt, options=opts, media=[], out_dir=runtime.output_dir())
        est = estimate_for("texture", props)
        job.estimate = est.credits if est else None
        job.extra.update({"targets": targets, "apply": bool(props.tex_apply), "seamless": bool(props.tex_seamless), "scale": float(props.tex_scale)})
        self._start(job, spec)
        return {"FINISHED"}


# ---------------- tasks ----------------


class _JobOp(bpy.types.Operator):
    bl_options = {"REGISTER", "INTERNAL"}
    job_id: StringProperty(options={"HIDDEN"})

    def job(self) -> Job | None:
        return runtime.JOBS.get(self.job_id)


class POPNOW_OT_job_confirm(_JobOp):
    bl_idname = "popnow.job_confirm"
    bl_label = "Start"
    bl_description = "Start this generation. Credits are held now and the unused part is returned when it finishes"

    def execute(self, context):
        runtime.confirm(self.job_id)
        return {"FINISHED"}


class POPNOW_OT_job_cancel(_JobOp):
    bl_idname = "popnow.job_cancel"
    bl_label = "Cancel"
    bl_description = "Before it starts: cancel, nothing is charged. After it starts: stop waiting here; the task finishes on PopNow and you can resume to fetch it"

    def execute(self, context):
        runtime.cancel(self.job_id)
        return {"FINISHED"}


class POPNOW_OT_job_resume(_JobOp):
    bl_idname = "popnow.job_resume"
    bl_label = "Resume"
    bl_description = "Check the task on PopNow again and download the result when it is ready"

    def execute(self, context):
        job = self.job()
        if job is None or not job.task_id:
            return {"CANCELLED"}
        if not _ready(self, context):
            return {"CANCELLED"}
        runtime.resume(job)
        return {"FINISHED"}


class POPNOW_OT_job_rerun(_JobOp):
    bl_idname = "popnow.job_rerun"
    bl_label = "Run again"
    bl_description = "Send the same inputs and settings again (you confirm the price first)"

    def execute(self, context):
        old = self.job()
        if old is None or not _ready(self, context):
            return {"CANCELLED"}
        missing = [m.path for m in old.media if not os.path.exists(m.path)]
        if missing:
            self.report({"ERROR"}, "The original input files are gone. Render again from the panel.")
            return {"CANCELLED"}
        spec = models.get(old.model)
        job = Job(workflow=old.workflow, model=old.model, label=old.label, prompt=old.prompt, user_prompt=old.user_prompt, options=dict(old.options), media=[MediaFile(m.path, m.slot, m.seconds) for m in old.media], out_dir=runtime.output_dir())
        job.extra = {k: v for k, v in old.extra.items() if not k.startswith(("apply", "texture_path", "seam"))}
        job.estimate = old.estimate
        job.extra["typical_seconds"] = spec.typical_seconds if spec else 60
        runtime.start(job, build_input=_build_input)
        return {"FINISHED"}


class POPNOW_OT_job_remove(_JobOp):
    bl_idname = "popnow.job_remove"
    bl_label = "Remove from list"
    bl_description = "Remove this entry from the task list (files on disk are kept)"

    def execute(self, context):
        job = self.job()
        if job and not job.is_active:
            runtime.JOBS.pop(job.id, None)
            runtime.save_history()
        return {"FINISHED"}


class POPNOW_OT_copy_task_id(_JobOp):
    bl_idname = "popnow.copy_task_id"
    bl_label = "Copy task ID"
    bl_description = "Copy the task ID (include it when you contact support)"

    def execute(self, context):
        job = self.job()
        if job and job.task_id:
            context.window_manager.clipboard = job.task_id
            self.report({"INFO"}, f"Copied {job.task_id}")
        return {"FINISHED"}


class POPNOW_OT_history_clear(bpy.types.Operator):
    bl_idname = "popnow.history_clear"
    bl_label = "Clear finished"
    bl_description = "Remove finished tasks from the list (files on disk are kept)"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        for jid in [j.id for j in runtime.JOBS.values() if not j.is_active and not j.resumable]:
            runtime.JOBS.pop(jid, None)
        runtime.save_history()
        return {"FINISHED"}


class POPNOW_OT_refresh_balance(bpy.types.Operator):
    bl_idname = "popnow.refresh_balance"
    bl_label = "Refresh balance"
    bl_description = "Check your credit balance"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        if not _ready(self, context):
            return {"CANCELLED"}
        runtime.refresh_balance()
        return {"FINISHED"}


class POPNOW_OT_open_prefs(bpy.types.Operator):
    bl_idname = "popnow.open_prefs"
    bl_label = "PopNow settings"
    bl_description = "Open the PopNow add-on preferences"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=runtime.ADDON_ID)
        except Exception:
            bpy.ops.screen.userpref_show()
        return {"FINISHED"}


class POPNOW_OT_open_online_prefs(bpy.types.Operator):
    bl_idname = "popnow.open_online_prefs"
    bl_label = "Open System preferences"
    bl_description = "Allow Online Access lives in Preferences > System"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        context.preferences.active_section = "SYSTEM"
        bpy.ops.screen.userpref_show()
        return {"FINISHED"}


# ---------------- results ----------------


class POPNOW_OT_result_show(_JobOp):
    bl_idname = "popnow.result_show"
    bl_label = "Show"
    bl_description = "Open the result in an Image Editor"
    index: IntProperty(default=0, options={"HIDDEN"})

    def execute(self, context):
        job = self.job()
        img = results.job_image(job, self.index) if job else None
        if img is None:
            self.report({"ERROR"}, "The result file is missing.")
            return {"CANCELLED"}
        results.show_in_image_editor(img)
        return {"FINISHED"}


class POPNOW_OT_result_camera_bg(_JobOp):
    bl_idname = "popnow.result_camera_bg"
    bl_label = "Camera background"
    bl_description = "Show the result over the camera view to compare it with your scene"

    def execute(self, context):
        job = self.job()
        cam = bpy.data.objects.get(job.extra.get("camera") or "") if job else None
        cam = cam if cam is not None and cam.type == "CAMERA" else context.scene.camera
        if job is None or cam is None or not job.outputs:
            self.report({"ERROR"}, "Needs a scene camera and a finished result.")
            return {"CANCELLED"}
        if job.workflow == "animate":
            src = bpy.data.movieclips.load(job.outputs[0], check_existing=True)
        else:
            src = results.job_image(job, 0)
        results.set_camera_background(src, cam)
        self.report({"INFO"}, f"Added to {cam.name}'s background. Look through the camera (Numpad 0) to see it.")
        return {"FINISHED"}


class POPNOW_OT_result_project(_JobOp):
    bl_idname = "popnow.result_project"
    bl_label = "Project onto selected"
    bl_description = "Project the result from the camera it was rendered from onto the selected meshes (adds a UV Project modifier and a material)"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        job = self.job()
        cam = bpy.data.objects.get(job.extra.get("camera") or "") if job else None
        if job is None or cam is None:
            self.report({"ERROR"}, "Projection needs the camera the render came from. Renders from the viewport angle cannot be projected.")
            return {"CANCELLED"}
        meshes = [o for o in context.selected_objects if o.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select the meshes to project onto.")
            return {"CANCELLED"}
        img = results.job_image(job, 0)
        w, h = job.extra.get("aspect") or (16, 9)
        k = max(w, h)
        n = results.project_from_camera(img, cam, meshes, (w / k, h / k))
        self.report({"INFO"}, f"Projected onto {n} object(s).")
        return {"FINISHED"}


class POPNOW_OT_result_vse(_JobOp):
    bl_idname = "popnow.result_vse"
    bl_label = "Add to Video Editor"
    bl_description = "Add the video to this scene's Video Sequencer at the current frame"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        job = self.job()
        if job is None or not job.outputs or not os.path.exists(job.outputs[0]):
            self.report({"ERROR"}, "The video file is missing.")
            return {"CANCELLED"}
        results.add_to_sequencer(job.outputs[0], context.scene, f"PopNow {job.label}")
        self.report({"INFO"}, "Added to the Video Sequencer.")
        return {"FINISHED"}


class POPNOW_OT_texture_apply(_JobOp):
    bl_idname = "popnow.texture_apply"
    bl_label = "Apply to selected"
    bl_description = "Put this texture on the selected objects (replaces the active material slot)"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        job = self.job()
        objs = [o for o in context.selected_objects if hasattr(getattr(o, "data", None), "materials")]
        if job is None or not objs:
            self.report({"ERROR"}, "Select the objects first.")
            return {"CANCELLED"}
        n = results.apply_texture(job, objs)
        self.report({"INFO"}, f"Applied to {n} object(s).")
        return {"FINISHED"}


class POPNOW_OT_result_open(_JobOp):
    bl_idname = "popnow.result_open"
    bl_label = "Open file"
    bl_description = "Open the downloaded file with your system's default app"

    def execute(self, context):
        job = self.job()
        if job and job.outputs:
            bpy.ops.wm.path_open(filepath=job.outputs[0])
        return {"FINISHED"}


class POPNOW_OT_result_folder(_JobOp):
    bl_idname = "popnow.result_folder"
    bl_label = "Open folder"
    bl_description = "Open the folder with the downloaded results"

    def execute(self, context):
        job = self.job()
        folder = os.path.dirname(job.outputs[0]) if job and job.outputs else runtime.output_dir()
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


class POPNOW_OT_result_save_as(_JobOp):
    bl_idname = "popnow.result_save_as"
    bl_label = "Save as…"
    bl_description = "Save a copy of the result somewhere else"
    filepath: StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        job = self.job()
        if job is None or not job.outputs:
            return {"CANCELLED"}
        src = job.extra.get("texture_path") or job.outputs[0]
        self.filepath = os.path.join(os.path.expanduser("~"), os.path.basename(src))
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        job = self.job()
        src = (job.extra.get("texture_path") or job.outputs[0]) if job and job.outputs else ""
        if not src or not os.path.exists(src):
            self.report({"ERROR"}, "The result file is missing.")
            return {"CANCELLED"}
        dest = bpy.path.abspath(self.filepath)
        if not os.path.splitext(dest)[1]:
            dest += os.path.splitext(src)[1]
        shutil.copyfile(src, dest)
        self.report({"INFO"}, f"Saved {os.path.basename(dest)}")
        return {"FINISHED"}


class POPNOW_OT_open_url(bpy.types.Operator):
    bl_idname = "popnow.open_url"
    bl_label = "Open"
    bl_options = {"REGISTER", "INTERNAL"}
    target: StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, context, props):
        return {"console": "Open the PopNow developer console to create an API key", "pricing": "Buy credits on popnow.ai"}.get(props.target, "Open popnow.ai")

    def execute(self, context):
        url = {"console": CONSOLE_URL, "pricing": PRICING_URL}.get(self.target, "https://popnow.ai/blender")
        bpy.ops.wm.url_open(url=url)
        return {"FINISHED"}


CLASSES = (
    POPNOW_OT_render,
    POPNOW_OT_animate,
    POPNOW_OT_texture,
    POPNOW_OT_job_confirm,
    POPNOW_OT_job_cancel,
    POPNOW_OT_job_resume,
    POPNOW_OT_job_rerun,
    POPNOW_OT_job_remove,
    POPNOW_OT_copy_task_id,
    POPNOW_OT_history_clear,
    POPNOW_OT_refresh_balance,
    POPNOW_OT_open_prefs,
    POPNOW_OT_open_online_prefs,
    POPNOW_OT_result_show,
    POPNOW_OT_result_camera_bg,
    POPNOW_OT_result_project,
    POPNOW_OT_result_vse,
    POPNOW_OT_texture_apply,
    POPNOW_OT_result_open,
    POPNOW_OT_result_folder,
    POPNOW_OT_result_save_as,
    POPNOW_OT_open_url,
)
