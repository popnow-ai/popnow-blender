"""Bring results into Blender on the main thread when a task finishes: load images / videos, show them in the Image Editor, set as camera background,
project as a texture, add to the Video Sequencer, build seamless texture materials."""

import os

import bpy

from . import runtime
from ..core.jobs import Job


def _short(text: str, n: int = 28) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def load_image(path: str, name: str) -> bpy.types.Image:
    img = bpy.data.images.load(path, check_existing=True)
    img.name = name
    return img


def job_image(job: Job, index: int = 0) -> bpy.types.Image | None:
    """The Image for the task's index-th output (loaded if not yet loaded)"""
    if index >= len(job.outputs) or not os.path.exists(job.outputs[index]):
        return None
    path = job.extra.get("texture_path") if job.workflow == "texture" and index == 0 and job.extra.get("texture_path") else job.outputs[index]
    for img in bpy.data.images:
        if img.filepath and os.path.normpath(bpy.path.abspath(img.filepath)) == os.path.normpath(path):
            return img
    return load_image(path, f"PopNow {job.workflow.title()} {_short(job.user_prompt or job.label, 20)}")


# The order for finding an Image Editor follows AI Render (benrugg/AI-Render, MIT, utils.find_area_showing_render_result / view_sd_in_render_view,
# license in licenses/AI-Render-MIT.txt): first the area showing the render result, then any area, then switch to the built-in Rendering workspace,
# and only open a new window when none exists (a new window is the most disruptive, so it comes last).
def _image_editors(scene=None):
    wm = bpy.context.window_manager
    out = []
    for win in wm.windows:
        if scene is not None and win.scene != scene:
            continue
        out.extend(a for a in win.screen.areas if a.type == "IMAGE_EDITOR")
    return out


def _pick_image_editor(scene=None):
    areas = _image_editors(scene)
    backup = None
    for area in areas:
        img = area.spaces.active.image
        if img is not None and img.type == "RENDER_RESULT":
            return area
        backup = backup or area
    return backup


def show_in_image_editor(img: bpy.types.Image, *, allow_new_window: bool = True) -> bool:
    scene = bpy.context.scene
    area = _pick_image_editor(scene) or _pick_image_editor()
    if area is None:
        ws = bpy.data.workspaces.get("Rendering")
        wm = bpy.context.window_manager
        if ws is not None and wm.windows:
            try:
                wm.windows[0].workspace = ws
                area = next((a for a in ws.screens[0].areas if a.type == "IMAGE_EDITOR"), None)
            except Exception:
                area = None
    if area is not None:
        area.spaces.active.image = img
        area.tag_redraw()
        return True
    wm = bpy.context.window_manager
    if not allow_new_window or not wm.windows:
        return False
    win = wm.windows[0]
    try:
        with bpy.context.temp_override(window=win, screen=win.screen, area=win.screen.areas[0]):
            bpy.ops.wm.window_new()
        new = wm.windows[-1]
        area = max(new.screen.areas, key=lambda a: a.width * a.height)
        area.ui_type = "IMAGE_EDITOR"
        area.spaces.active.image = img
        return True
    except Exception as e:
        print(f"PopNow: could not open an Image Editor window: {e}")
        return False


def set_camera_background(img_or_clip, cam_obj: bpy.types.Object) -> None:
    """Set as camera background (in front, semi-transparent, so it can be compared with the clay scene through the camera)"""
    cam = cam_obj.data
    bg = next((b for b in cam.background_images if (b.image == img_or_clip or b.clip == img_or_clip)), None)
    if bg is None:
        bg = cam.background_images.new()
    if isinstance(img_or_clip, bpy.types.MovieClip):
        bg.source = "MOVIE_CLIP"
        bg.clip = img_or_clip
    else:
        bg.source = "IMAGE"
        bg.image = img_or_clip
    bg.alpha = 0.6
    bg.display_depth = "FRONT"
    bg.frame_method = "FIT"
    cam.show_background_images = True


def project_from_camera(img: bpy.types.Image, cam_obj: bpy.types.Object, objects: list[bpy.types.Object], aspect: tuple[float, float]) -> int:
    """Camera projection texture: add a UV layer and a UV Project modifier (projector = this camera) to each mesh, and assign a material using this image"""
    mat = bpy.data.materials.new(f"PopNow Projection {_short(img.name, 18)}")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.extension = "CLIP"
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "PopNow Projection"
    nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    if bsdf:
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        tex.location = (bsdf.location.x - 320, bsdf.location.y)
        uv.location = (tex.location.x - 220, tex.location.y)
    n = 0
    for ob in objects:
        if ob.type != "MESH":
            continue
        me = ob.data
        if "PopNow Projection" not in me.uv_layers:
            me.uv_layers.new(name="PopNow Projection")
        mod = ob.modifiers.get("PopNow Projection") or ob.modifiers.new("PopNow Projection", "UV_PROJECT")
        mod.uv_layer = "PopNow Projection"
        mod.projector_count = 1
        mod.projectors[0].object = cam_obj
        mod.aspect_x, mod.aspect_y = aspect
        me.materials.append(mat)
        idx = len(me.materials) - 1
        for poly in me.polygons:
            poly.material_index = idx
        n += 1
    return n


def texture_material(img: bpy.types.Image, name: str, scale: float) -> bpy.types.Material:
    """Seamless texture material: use UVs when present, otherwise box-project by object coordinates; tiling count = scale"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.extension = "REPEAT"
    tex.projection = "BOX"
    tex.projection_blend = 0.2
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
    if bsdf:
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        tex.location = (bsdf.location.x - 320, bsdf.location.y)
        mapping.location = (tex.location.x - 220, tex.location.y)
        coord.location = (mapping.location.x - 220, mapping.location.y)
    return mat


def make_texture(job: Job) -> bpy.types.Image:
    """Make the downloaded texture seamless (if enabled), save it as PNG, return the Image"""
    from ..core import imaging
    from .capture import load_pixels, save_pixels

    src = job.outputs[0]
    name = f"PopNow Texture {_short(job.user_prompt, 24)}"
    if job.extra.get("seamless", True):
        arr = load_pixels(src)
        tiled = imaging.make_tileable(arr)
        base = os.path.splitext(src)[0]
        path = base + "_seamless.png"
        img = save_pixels(tiled, path, name=name, keep=True)
        job.extra["texture_path"] = path
        job.extra["seam_error"] = round(imaging.seam_error(tiled), 4)
        return img
    job.extra["texture_path"] = src
    return load_image(src, name)


def apply_texture(job: Job, objects: list[bpy.types.Object]) -> int:
    img = job_image(job, 0)
    if img is None:
        return 0
    mat = texture_material(img, f"PopNow {_short(job.user_prompt, 24)}", float(job.extra.get("scale", 1.0)))
    n = 0
    for ob in objects:
        if not hasattr(ob.data, "materials"):
            continue
        if ob.material_slots and ob.active_material_index < len(ob.material_slots):
            ob.material_slots[ob.active_material_index].material = mat
        else:
            ob.data.materials.append(mat)
        n += 1
    return n


def add_to_sequencer(path: str, scene: bpy.types.Scene, name: str) -> object:
    scene.sequence_editor_create()
    se = scene.sequence_editor
    # since Blender 5.0 this is called strips (an empty collection is falsy, so `or` cannot be used)
    coll = se.strips if hasattr(se, "strips") else se.sequences
    existing = se.strips_all if hasattr(se, "strips_all") else getattr(se, "sequences_all", [])
    channel = min(128, max([s.channel for s in existing], default=0) + 1)
    return coll.new_movie(name, path, channel, scene.frame_current)


def apply(job: Job) -> None:
    """Called by the runtime timer when a task succeeds"""
    p = runtime.prefs()
    if job.workflow == "texture":
        img = make_texture(job)
        targets = [bpy.data.objects.get(n) for n in job.extra.get("targets", [])]
        targets = [t for t in targets if t is not None]
        if job.extra.get("apply") and targets:
            job.extra["applied_to"] = apply_texture(job, targets)
        if getattr(p, "open_results", True):
            show_in_image_editor(img)
        return
    if job.workflow == "render":
        # load it whether or not a window opens, so it can be picked in any Image Editor / material
        img = job_image(job, 0)
        if img is not None and getattr(p, "open_results", True):
            show_in_image_editor(img)
        return
    # Video: only downloaded locally and loaded as a MovieClip (adding to the VSE or camera background is done by buttons)
    if job.outputs:
        try:
            clip = bpy.data.movieclips.load(job.outputs[0], check_existing=True)
            clip.name = f"PopNow Video {_short(job.user_prompt or job.label, 20)}"
        except RuntimeError as e:
            job.extra["apply_error"] = str(e)
