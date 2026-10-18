"""N panel (the PopNow tab in the 3D viewport sidebar): Account, Render with AI, Animate with AI, Texture, Tasks.

Built so the defaults just work: each block shows only the prompt, style, reference images, model and one main button with the price;
rarely used settings such as aspect ratio, resolution and structure pass sit in a More subpanel collapsed by default. The Tasks panel also appears in the Image Editor sidebar.
"""

import time

import bpy

from . import capture, runtime
from .ops import animate_seconds, estimate_for
from ..core import models
from ..core.client import mask_key

CATEGORY = "PopNow"
STATE_ICON = {
    "uploading": "EXPORT",
    "quoting": "TIME",
    "awaiting_confirm": "QUESTION",
    "submitting": "TIME",
    "queued": "SORTTIME",
    "running": "RENDER_ANIMATION",
    "downloading": "IMPORT",
    "succeeded": "CHECKMARK",
    "failed": "ERROR",
    "canceled": "X",
    "stopped": "PAUSE",
}


def _price_text(label: str, est) -> str:
    if est is None:
        return label
    return f"{label} · {'~' if est.approx else '≈'}{est.credits} credits"


def _blocked(layout) -> bool:
    """Draw a hint when online access or the key is missing; returns True when the main button must be disabled"""
    if not runtime.online_allowed():
        return True
    return runtime.key_status() != "ok"


def _prompt(layout, props, attr: str, placeholder: str) -> None:
    """Prompt field; older Blender versions lack the placeholder argument, so fall back to the call without it"""
    try:
        layout.prop(props, attr, text="", placeholder=placeholder)
    except TypeError:
        layout.prop(props, attr, text="")


def _note(layout, text: str, icon: str = "INFO") -> None:
    col = layout.column()
    col.scale_y = 0.8
    label_multiline(col, bpy.context, text, icon=icon, max_lines=3)


class _Panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY


class POPNOW_PT_account(_Panel):
    bl_label = "PopNow"
    bl_idname = "POPNOW_PT_account"
    bl_order = 0

    def draw_header_preset(self, context):
        self.layout.operator("popnow.open_prefs", text="", icon="PREFERENCES", emboss=False)

    def draw(self, context):
        layout = self.layout
        runtime.load_history()
        if not runtime.online_allowed():
            box = layout.box()
            box.label(text="Online access is off", icon="ERROR")
            box.label(text="PopNow needs it to send renders.")
            box.operator("popnow.open_online_prefs", icon="PREFERENCES")
            return
        p = runtime.prefs()
        status = runtime.key_status()
        if status != "ok":
            col = layout.column(align=True)
            col.label(text="1. Get a free API key", icon="KEYINGSET")
            col.operator("popnow.open_url", text="Get API key", icon="URL").target = "console"
            col.separator()
            col.label(text="2. Paste it here")
            if p is not None:
                col.prop(p, "api_key", text="")
            if status == "malformed":
                col.label(text="Keys start with sk-popnow-", icon="ERROR")
            return
        row = layout.row(align=True)
        b = runtime.BALANCE
        if b.get("loading"):
            row.label(text="Checking balance…", icon="TIME")
        elif b.get("error"):
            row.alert = True
            row.label(text=b["error"][:60], icon="ERROR")
        elif "credits" in b:
            held = f" · {b['held']} held" if b.get("held") else ""
            row.label(text=f"{b['credits']:,} credits{held}", icon="FUND")
        else:
            row.label(text=f"Key {mask_key(runtime.api_key())}", icon="CHECKMARK")
        row.operator("popnow.refresh_balance", text="", icon="FILE_REFRESH")
        row.operator("popnow.open_url", text="", icon="ADD").target = "pricing"
        if not b and status == "ok":
            # fetch the balance once when the panel is first drawn (background thread, does not block the UI)
            runtime.refresh_balance()


class POPNOW_PT_render(_Panel):
    bl_label = "Render with AI"
    bl_idname = "POPNOW_PT_render"
    bl_order = 1

    def draw_header(self, context):
        self.layout.label(text="", icon="RESTRICT_RENDER_OFF")

    def draw(self, context):
        layout = self.layout
        props = context.scene.popnow
        spec = models.get(props.render_model)
        layout.row().prop(props, "render_source", expand=True)
        _prompt(layout, props, "render_prompt", "A cozy cabin at dusk, warm window light")
        layout.prop(props, "render_style")
        layout.prop(props, "render_model")
        if spec:
            _note(layout, spec.note)
        if spec and spec.max_refs and not spec.media("control"):
            col = layout.column(align=True)
            col.label(text="Reference images (optional)")
            for i, attr in enumerate(("ref_1", "ref_2", "ref_3")[: spec.max_refs]):
                if i == 0 or getattr(props, ("ref_1", "ref_2", "ref_3")[i - 1]) is not None:
                    col.template_ID(props, attr, open="image.open")
        est = estimate_for("render", props)
        row = layout.row()
        row.scale_y = 1.5
        row.enabled = not _blocked(layout)
        row.operator("popnow.render", text=_price_text("Render with AI", est), icon="SHADING_RENDERED")
        if props.render_source in ("CAMERA", "RENDER") and context.scene.camera is None:
            _note(layout, "Add a camera, or switch to Viewport", "ERROR")


class POPNOW_PT_render_more(_Panel):
    bl_label = "More"
    bl_parent_id = "POPNOW_PT_render"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        props = context.scene.popnow
        spec = models.get(props.render_model)
        if spec is None:
            return
        if spec.option("aspect"):
            layout.prop(props, "render_aspect")
        if spec.option("resolution"):
            layout.prop(props, "render_resolution")
        if spec.option("quality"):
            layout.prop(props, "render_quality")
        layout.prop(props, "render_look")
        layout.prop(props, "render_pass")
        if spec.media("control"):
            layout.prop(props, "control_strength")
            if props.render_pass == "none":
                _note(layout, "This model always uses a depth pass")


class POPNOW_PT_animate(_Panel):
    bl_label = "Animate with AI"
    bl_idname = "POPNOW_PT_animate"
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.label(text="", icon="CAMERA_DATA")

    def draw(self, context):
        layout = self.layout
        props = context.scene.popnow
        spec = models.get(props.anim_model)
        layout.row().prop(props, "source", expand=True)
        _prompt(layout, props, "anim_prompt", "Two friends walk through a park in autumn")
        layout.prop(props, "anim_style")
        layout.prop(props, "anim_model")
        if spec is None:
            return
        _note(layout, spec.note)
        if spec.media("character"):
            layout.label(text="Character (required)")
            layout.template_ID(props, "anim_character", open="image.open")
        if spec.media("refs"):
            col = layout.column(align=True)
            col.label(text="Reference images (optional)")
            col.template_ID(props, "anim_ref_1", open="image.open")
            if props.anim_ref_1 is not None and spec.max_refs > 1:
                col.template_ID(props, "anim_ref_2", open="image.open")
        row = layout.row(align=True)
        if spec.uses_duration_option:
            row.prop(props, "anim_duration")
        else:
            row.prop(props, "anim_clip_seconds")
        if spec.option("resolution"):
            row.prop(props, "anim_resolution", text="")
        secs = animate_seconds(spec, props)
        _note(layout, capture.frames_note(context.scene, secs), "SEQUENCE")
        est = estimate_for("animate", props)
        row = layout.row()
        row.scale_y = 1.5
        row.enabled = not _blocked(layout)
        row.operator("popnow.animate", text=_price_text("Animate with AI", est), icon="RENDER_ANIMATION")


class POPNOW_PT_animate_more(_Panel):
    bl_label = "More"
    bl_parent_id = "POPNOW_PT_animate"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        props = context.scene.popnow
        spec = models.get(props.anim_model)
        if spec is None:
            return
        if spec.option("aspect"):
            layout.prop(props, "anim_aspect")
        if spec.option("audio"):
            layout.prop(props, "anim_audio")
        if spec.option("animation_mode"):
            layout.prop(props, "anim_animation_mode")
        layout.prop(props, "anim_look")
        _note(layout, "The preview is a quick Workbench render of your animation")


class POPNOW_PT_texture(_Panel):
    bl_label = "Texture"
    bl_idname = "POPNOW_PT_texture"
    bl_order = 3
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.label(text="", icon="TEXTURE")

    def draw(self, context):
        layout = self.layout
        props = context.scene.popnow
        _prompt(layout, props, "tex_prompt", "Mossy cobblestone")
        layout.prop(props, "tex_style")
        row = layout.row(align=True)
        row.prop(props, "tex_model", text="")
        row.prop(props, "tex_resolution", text="")
        col = layout.column(align=True)
        col.prop(props, "tex_seamless")
        col.prop(props, "tex_apply")
        if props.tex_apply:
            col.prop(props, "tex_scale")
            n = len([o for o in context.selected_objects if hasattr(getattr(o, "data", None), "materials")])
            _note(layout, f"{n} selected object(s)" if n else "Nothing selected: the texture is only saved", "OBJECT_DATA")
        est = estimate_for("texture", props)
        row = layout.row()
        row.scale_y = 1.5
        row.enabled = not _blocked(layout) and bool(props.tex_prompt.strip())
        row.operator("popnow.texture", text=_price_text("Generate Texture", est), icon="TEXTURE")


def draw_tasks(layout, context) -> None:
    runtime.load_history()
    props = context.scene.popnow
    jobs = runtime.jobs_sorted()
    if not jobs:
        layout.label(text="Results show up here.", icon="INFO")
        return
    shown = jobs if props.show_all_history else jobs[:6]
    for job in shown:
        box = layout.box()
        head = box.row(align=True)
        head.label(text=f"{job.workflow.title()} · {job.label}", icon=STATE_ICON.get(job.state, "DOT"))
        if job.credits is not None and job.state in ("succeeded", "failed") and job.settled:
            head.label(text=f"{job.credits} cr")
        if not job.is_active:
            op = head.operator("popnow.job_remove", text="", icon="X", emboss=False)
            op.job_id = job.id
        if job.user_prompt:
            box.label(text=job.user_prompt[:60])
        if job.is_active and job.state != "awaiting_confirm":
            box.progress(factor=max(0.02, min(job.progress, 1.0)), text=job.status[:70])
        else:
            label_multiline(box, context, job.status, max_lines=4, alert=job.state == "failed")
        if job.state == "awaiting_confirm":
            row = box.row(align=True)
            row.scale_y = 1.3
            op = row.operator("popnow.job_confirm", text=f"Start · {job.max_credits} credits", icon="PLAY")
            op.job_id = job.id
            row.operator("popnow.job_cancel", text="Cancel").job_id = job.id
            if job.expected_credits is not None and job.expected_credits != job.max_credits:
                box.label(text=f"Usually {job.expected_credits}. Unused credits come back.")
        elif job.is_active:
            row = box.row(align=True)
            label = "Cancel" if not job.task_id else "Stop waiting"
            row.operator("popnow.job_cancel", text=label, icon="CANCEL").job_id = job.id
            if job.task_id:
                box.label(text="Stopping only stops waiting here; the task still runs.", icon="INFO")
        elif job.resumable:
            row = box.row(align=True)
            row.operator("popnow.job_resume", icon="PLAY").job_id = job.id
            row.operator("popnow.copy_task_id", text="", icon="COPYDOWN").job_id = job.id
        elif job.state == "succeeded":
            _result_buttons(box, job)
        elif job.state == "failed":
            row = box.row(align=True)
            row.operator("popnow.job_rerun", icon="FILE_REFRESH").job_id = job.id
            if job.task_id:
                row.operator("popnow.copy_task_id", text="", icon="COPYDOWN").job_id = job.id
            if job.error_code == "content_rejected":
                box.label(text="Try different wording or reference images.")
    if len(jobs) > 6:
        layout.prop(props, "show_all_history", text=f"Show all ({len(jobs)})", toggle=True)
    layout.operator("popnow.history_clear", icon="TRASH")


def _result_buttons(box, job) -> None:
    if job.extra.get("apply_error"):
        box.label(text="Could not load it into Blender; open the file instead.", icon="ERROR")
    grid = box.grid_flow(columns=2, align=True)
    if job.workflow in ("render", "texture"):
        grid.operator("popnow.result_show", text="Show", icon="IMAGE_DATA").job_id = job.id
    if job.workflow == "render":
        grid.operator("popnow.result_camera_bg", text="Camera bg", icon="CAMERA_DATA").job_id = job.id
        if job.extra.get("camera"):
            grid.operator("popnow.result_project", text="Project", icon="MOD_UVPROJECT").job_id = job.id
    if job.workflow == "texture":
        grid.operator("popnow.texture_apply", text="Apply", icon="MATERIAL").job_id = job.id
    if job.workflow == "animate":
        grid.operator("popnow.result_vse", text="To Video Editor", icon="SEQUENCE").job_id = job.id
        grid.operator("popnow.result_camera_bg", text="Camera bg", icon="CAMERA_DATA").job_id = job.id
        grid.operator("popnow.result_open", text="Play", icon="PLAY").job_id = job.id
    grid.operator("popnow.result_save_as", text="Save as…", icon="FILE_TICK").job_id = job.id
    grid.operator("popnow.result_folder", text="Folder", icon="FILE_FOLDER").job_id = job.id
    grid.operator("popnow.job_rerun", text="Again", icon="FILE_REFRESH").job_id = job.id
    if job.finished_at:
        box.label(text=f"Finished {_ago(job.finished_at)}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def label_multiline(layout, context, text: str, *, icon: str = "NONE", max_lines: int = 4, alert: bool = False) -> None:
    """Help text wrapped to the actual sidebar width. Characters per line = region width / 5.7, an estimate taken from AI Render's utils.label_multiline
    (benrugg/AI-Render, MIT, see licenses/AI-Render-MIT.txt); Blender labels do not wrap on their own"""
    region = getattr(context, "region", None)
    width = getattr(region, "width", 0) or 0
    scale = getattr(getattr(context, "preferences", None), "system", None)
    ui = getattr(scale, "ui_scale", 1.0) or 1.0
    chars = max(18, int(width / (5.7 * ui)) - 4) if width else 40
    for i, line in enumerate(_wrap(text, chars)[:max_lines]):
        row = layout.row()
        row.alert = alert
        row.label(text=line, icon=icon if i == 0 else "NONE")


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


class POPNOW_PT_tasks(_Panel):
    bl_label = "Tasks"
    bl_idname = "POPNOW_PT_tasks"
    bl_order = 4

    def draw_header(self, context):
        active = sum(1 for j in runtime.JOBS.values() if j.is_active)
        self.layout.label(text=f"({active})" if active else "", icon="SORTTIME")

    def draw(self, context):
        draw_tasks(self.layout, context)


class POPNOW_PT_tasks_image(bpy.types.Panel):
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "PopNow Tasks"
    bl_idname = "POPNOW_PT_tasks_image"

    def draw(self, context):
        draw_tasks(self.layout, context)


CLASSES = (
    POPNOW_PT_account,
    POPNOW_PT_render,
    POPNOW_PT_render_more,
    POPNOW_PT_animate,
    POPNOW_PT_animate_more,
    POPNOW_PT_texture,
    POPNOW_PT_tasks,
    POPNOW_PT_tasks_image,
)
