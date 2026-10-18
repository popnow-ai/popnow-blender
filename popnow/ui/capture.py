"""Render the scene into clay inputs: clay render (Workbench), depth / normal / line art structure passes, clay reference video.

Every step renders synchronously on the main thread (low-resolution Workbench usually takes a second or two; structure passes use Cycles at 1 sample with an emission override material).
Every scene setting changed before rendering is recorded and restored afterwards, leaving no trace (camera, engine, resolution, color management, output path, frame range...).
When framing from the viewport, a temporary camera matching the viewport is created and deleted afterwards.
"""

import glob
import os
from contextlib import contextmanager
from typing import Iterator

import bpy
from mathutils import Vector

# ---------------- temporary setting overrides ----------------


class _Overrides:
    """Record (object, attribute, original value) and restore in reverse order on exit"""

    def __init__(self) -> None:
        self._saved: list[tuple[object, str, object]] = []

    def set(self, obj, attr: str, value) -> bool:
        if obj is None or not hasattr(obj, attr):
            return False
        try:
            old = getattr(obj, attr)
            setattr(obj, attr, value)
        except (AttributeError, TypeError, ValueError):
            return False
        self._saved.append((obj, attr, old))
        return True

    def restore(self) -> None:
        for obj, attr, old in reversed(self._saved):
            try:
                setattr(obj, attr, old)
            except (AttributeError, TypeError, ValueError, ReferenceError):
                pass
        self._saved.clear()


@contextmanager
def overrides() -> Iterator[_Overrides]:
    o = _Overrides()
    try:
        yield o
    finally:
        o.restore()


# ---------------- camera ----------------


def _view3d(context):
    """(area, space, region_3d) of the current (or first) 3D viewport"""
    area = context.area if context.area and context.area.type == "VIEW_3D" else None
    if area is None and context.screen:
        area = next((a for a in context.screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        return None, None, None
    space = area.spaces.active
    return area, space, space.region_3d


def viewport_size(context) -> tuple[int, int] | None:
    area, _, _ = _view3d(context)
    if area is None:
        return None
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    return (region.width, region.height) if region else (area.width, area.height)


@contextmanager
def capture_camera(context, source: str) -> Iterator[tuple[bpy.types.Object, tuple[int, int]]]:
    """Return (camera object, frame width and height). With source=VIEWPORT a temporary camera matching the viewport is created; delete it afterwards"""
    scene = context.scene
    if source == "VIEWPORT":
        area, space, rv3d = _view3d(context)
        if rv3d is not None and rv3d.view_perspective == "CAMERA" and scene.camera:
            source = "CAMERA"  # the viewport is already looking through the camera
        elif rv3d is None:
            if not scene.camera:
                raise RuntimeError("No 3D viewport and no scene camera to render from.")
            source = "CAMERA"
    if source == "CAMERA":
        if not scene.camera:
            raise RuntimeError("The scene has no camera. Add one (Shift+A > Camera) or switch View to Viewport.")
        r = scene.render
        w = r.resolution_x * r.pixel_aspect_x
        h = r.resolution_y * r.pixel_aspect_y
        yield scene.camera, (int(w), int(h))
        return
    data = bpy.data.cameras.new("PopNow View (temp)")
    cam = bpy.data.objects.new("PopNow View (temp)", data)
    scene.collection.objects.link(cam)
    try:
        cam.matrix_world = rv3d.view_matrix.inverted()
        if rv3d.view_perspective == "ORTHO" or not rv3d.is_perspective:
            data.type = "ORTHO"
            # The viewport's orthographic extent is about view_distance x 2 (scaled by the viewport's long side)
            data.ortho_scale = rv3d.view_distance * 2.0
        else:
            # Viewport focal length assumes a 72mm sensor (the Blender viewport defaults to a 36mm sensor at 2x zoom)
            data.sensor_fit = "AUTO"
            data.sensor_width = 72.0
            data.lens = space.lens
        data.clip_start = max(space.clip_start, 0.001)
        data.clip_end = space.clip_end
        size = viewport_size(context) or (16, 9)
        yield cam, size
    finally:
        bpy.data.objects.remove(cam, do_unlink=True)
        bpy.data.cameras.remove(data, do_unlink=True)


def fit_size(aspect_wh: tuple[int, int], long_side: int, *, short_min: int = 0, multiple: int = 8) -> tuple[int, int]:
    """Scale the long side to long_side (short side at least short_min), rounded to a multiple of multiple (video encoding needs even sizes)"""
    w, h = aspect_wh
    w, h = max(w, 1), max(h, 1)
    scale = long_side / max(w, h)
    if short_min and min(w, h) * scale < short_min:
        scale = short_min / min(w, h)
    rw = max(multiple, int(round(w * scale / multiple)) * multiple)
    rh = max(multiple, int(round(h * scale / multiple)) * multiple)
    return rw, rh


# ---------------- generic rendering ----------------


def _engine_ids() -> set[str]:
    return {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}


def _view_transforms(scene) -> set[str]:
    try:
        return {i.identifier for i in scene.view_settings.bl_rna.properties["view_transform"].enum_items}
    except Exception:
        return set()


def _base_overrides(o: _Overrides, scene, cam, size: tuple[int, int], filepath: str) -> None:
    r = scene.render
    o.set(scene, "camera", cam)
    o.set(r, "resolution_x", size[0])
    o.set(r, "resolution_y", size[1])
    o.set(r, "resolution_percentage", 100)
    o.set(r, "pixel_aspect_x", 1.0)
    o.set(r, "pixel_aspect_y", 1.0)
    o.set(r, "use_border", False)
    o.set(r, "filepath", filepath)
    o.set(r, "use_file_extension", True)
    o.set(r, "use_stamp", False)
    o.set(r, "use_compositing", False)
    o.set(r, "use_sequencer", False)


def _workbench_look(o: _Overrides, scene, look: str) -> None:
    sh = scene.display.shading
    o.set(sh, "light", "STUDIO")
    o.set(sh, "show_shadows", False)
    o.set(sh, "show_object_outline", True)
    o.set(sh, "show_xray", False)
    o.set(sh, "show_backface_culling", False)
    if look == "CLAY":
        o.set(sh, "color_type", "SINGLE")
        o.set(sh, "single_color", (0.72, 0.72, 0.72))
        o.set(sh, "show_cavity", True)
        o.set(sh, "cavity_type", "BOTH")
    elif look == "OBJECT":
        o.set(sh, "color_type", "OBJECT")
        o.set(sh, "show_cavity", True)
    elif look == "MATERIAL":
        o.set(sh, "color_type", "MATERIAL")
    else:
        o.set(sh, "color_type", "TEXTURE")
    # Workbench's background is the world color: switch to light grey so the clay stands apart from the background
    if scene.world is not None:
        o.set(scene.world, "color", (0.32, 0.32, 0.34))
    o.set(scene.render, "film_transparent", False)
    vts = _view_transforms(scene)
    if "Standard" in vts:
        o.set(scene.view_settings, "view_transform", "Standard")
        o.set(scene.view_settings, "look", "None")
    o.set(scene.view_settings, "exposure", 0.0)
    o.set(scene.view_settings, "gamma", 1.0)


def _png_settings(o: _Overrides, scene, *, rgba: bool, depth16: bool) -> None:
    s = scene.render.image_settings
    if hasattr(s, "media_type"):
        o.set(s, "media_type", "IMAGE")
    o.set(s, "file_format", "PNG")
    o.set(s, "color_mode", "RGBA" if rgba else "RGB")
    o.set(s, "color_depth", "16" if depth16 else "8")
    o.set(s, "compression", 15)


def render_clay_still(context, cam, size_wh: tuple[int, int], look: str, out_base: str, long_side: int = 1024) -> str:
    """Clay render: one Workbench PNG; returns the file path"""
    scene = context.scene
    size = fit_size(size_wh, long_side)
    with overrides() as o:
        _base_overrides(o, scene, cam, size, out_base)
        o.set(scene.render, "engine", "BLENDER_WORKBENCH")
        _workbench_look(o, scene, look)
        _png_settings(o, scene, rgba=False, depth16=False)
        bpy.ops.render.render(write_still=True)
    return _written(out_base, ".png")


def _written(base: str, ext: str) -> str:
    path = base + ext if not base.lower().endswith(ext) else base
    if not os.path.exists(path):
        raise RuntimeError(f"Blender did not write {os.path.basename(path)}")
    return path


# ---------------- structure passes (depth / normal) ----------------


def _depth_range(scene, cam) -> tuple[float, float]:
    """Nearest / farthest distance of visible geometry from the camera (along the view axis), to normalize the depth map"""
    inv = cam.matrix_world.inverted()
    zs: list[float] = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for ob in scene.objects:
        if ob.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"}:
            continue
        if not ob.visible_get():
            continue
        ev = ob.evaluated_get(depsgraph)
        for corner in ev.bound_box:
            p = inv @ (ev.matrix_world @ Vector(corner))
            if p.z < 0:
                zs.append(-p.z)
    data = cam.data
    if not zs:
        return max(data.clip_start, 0.01), min(data.clip_end, 100.0)
    near = max(min(zs) * 0.95, data.clip_start)
    far = min(max(zs) * 1.05, data.clip_end)
    if far - near < 1e-3:
        far = near + 1.0
    return near, far


def _pass_material(kind: str, cam, near: float, far: float):
    """Emission override material: depth outputs near white / far black; normal outputs camera-space normals x0.5+0.5 (facing the lens is blue)"""
    mat = bpy.data.materials.new(f"PopNow {kind} (temp)")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    if kind == "depth":
        cd = nt.nodes.new("ShaderNodeCameraData")
        mr = nt.nodes.new("ShaderNodeMapRange")
        mr.clamp = True
        mr.inputs["From Min"].default_value = near
        mr.inputs["From Max"].default_value = far
        mr.inputs["To Min"].default_value = 1.0
        mr.inputs["To Max"].default_value = 0.0
        z_out = cd.outputs.get("View Z Depth") or cd.outputs[1]
        nt.links.new(z_out, mr.inputs["Value"])
        nt.links.new(mr.outputs["Result"], em.inputs["Color"])
    else:
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        comb = nt.nodes.new("ShaderNodeCombineXYZ")
        m = cam.matrix_world.to_3x3().normalized()
        axes = [m.col[0], m.col[1], m.col[2]]  # right, up, towards the lens (the camera looks along local -Z)
        for i, axis in enumerate(axes):
            dot = nt.nodes.new("ShaderNodeVectorMath")
            dot.operation = "DOT_PRODUCT"
            nt.links.new(geo.outputs["Normal"], dot.inputs[0])
            dot.inputs[1].default_value = tuple(axis)
            nt.links.new(dot.outputs["Value"], comb.inputs[i])
        ma = nt.nodes.new("ShaderNodeVectorMath")
        ma.operation = "MULTIPLY_ADD"
        nt.links.new(comb.outputs[0], ma.inputs[0])
        ma.inputs[1].default_value = (0.5, 0.5, 0.5)
        ma.inputs[2].default_value = (0.5, 0.5, 0.5)
        nt.links.new(ma.outputs[0], em.inputs["Color"])
    return mat


def render_pass(context, cam, size_wh: tuple[int, int], kind: str, out_base: str, long_side: int = 1024) -> str:
    """depth / normal: Cycles at 1 sample + view layer override material, Raw color (values not changed by tone mapping), PNG with transparency"""
    scene = context.scene
    size = fit_size(size_wh, long_side)
    engines = _engine_ids()
    if "CYCLES" not in engines:
        raise RuntimeError("Structure passes need the Cycles render engine (enable it in Preferences > Add-ons).")
    near, far = _depth_range(scene, cam)
    mat = _pass_material(kind, cam, near, far)
    try:
        with overrides() as o:
            _base_overrides(o, scene, cam, size, out_base)
            o.set(scene.render, "engine", "CYCLES")
            cy = getattr(scene, "cycles", None)
            o.set(cy, "samples", 1)
            o.set(cy, "preview_samples", 1)
            o.set(cy, "use_adaptive_sampling", False)
            o.set(cy, "use_denoising", False)
            o.set(cy, "max_bounces", 0)
            o.set(scene.render, "film_transparent", True)
            o.set(context.view_layer, "material_override", mat)
            vts = _view_transforms(scene)
            o.set(scene.view_settings, "view_transform", "Raw" if "Raw" in vts else "Standard")
            o.set(scene.view_settings, "look", "None")
            o.set(scene.view_settings, "exposure", 0.0)
            o.set(scene.view_settings, "gamma", 1.0)
            _png_settings(o, scene, rgba=True, depth16=True)
            bpy.ops.render.render(write_still=True)
    finally:
        bpy.data.materials.remove(mat, do_unlink=True)
    return _written(out_base, ".png")


# ---------------- pixel I/O (numpy) ----------------


def load_pixels(path: str):
    """Read an image into a numpy array (top row first, RGBA, 0..1)"""
    import numpy as np

    img = bpy.data.images.load(path, check_existing=False)
    try:
        img.colorspace_settings.name = "Non-Color"
        w, h = img.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(h, w, 4)[::-1].copy()
    finally:
        bpy.data.images.remove(img)


def save_pixels(arr, path: str, *, name: str = "PopNow (temp)", keep: bool = False, colorspace: str = "sRGB"):
    """Save a numpy array (top row first, RGB or RGBA) as PNG; with keep=True keep the Image and return it"""
    import numpy as np

    h, w = arr.shape[:2]
    if arr.shape[2] == 3:
        arr = np.concatenate([arr, np.ones((h, w, 1), np.float32)], axis=2)
    img = bpy.data.images.new(name, w, h, alpha=True, float_buffer=False)
    try:
        img.colorspace_settings.name = colorspace
    except TypeError:
        pass
    img.pixels.foreach_set(np.ascontiguousarray(arr[::-1], dtype=np.float32).ravel())
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    if keep:
        img.filepath = path
        return img
    bpy.data.images.remove(img)
    return None


def structure_image(context, cam, size_wh, kind: str, out_dir: str) -> str:
    """Generate one structure pass (depth / normal / lineart) on a background color; returns the PNG path"""
    from ..core import imaging

    if kind in ("depth", "normal"):
        raw = render_pass(context, cam, size_wh, kind, os.path.join(out_dir, f"{kind}-raw"))
        arr = load_pixels(raw)
        bg = (0.0, 0.0, 0.0) if kind == "depth" else (0.5, 0.5, 1.0)
        rgb = imaging.composite(arr, bg)
        path = os.path.join(out_dir, f"{kind}.png")
        save_pixels(rgb, path, colorspace="Non-Color")
        return path
    if kind == "lineart":
        d = load_pixels(render_pass(context, cam, size_wh, "depth", os.path.join(out_dir, "depth-raw")))
        n = load_pixels(render_pass(context, cam, size_wh, "normal", os.path.join(out_dir, "normal-raw")))
        lines = imaging.lineart(imaging.composite(d, (0, 0, 0)), imaging.composite(n, (0.5, 0.5, 1.0)))
        path = os.path.join(out_dir, "lineart.png")
        save_pixels(lines, path, colorspace="Non-Color")
        return path
    raise ValueError(kind)


# ---------------- clay video ----------------


def render_clay_clip(context, cam, size_wh, look: str, seconds: float, out_dir: str, *, short_side: int = 720, long_max: int = 1280) -> tuple[str, float]:
    """Render a seconds-long Workbench preview video (H.264 MP4) from the scene start frame; returns (path, actual seconds)"""
    scene = context.scene
    r = scene.render
    fps = r.fps / (r.fps_base or 1.0)
    frames = max(2, int(round(seconds * fps)))
    w, h = size_wh
    # Short side at least short_side (video restyle models require >= 720 per side), long side capped
    size = fit_size((w, h), long_max, short_min=short_side, multiple=2)
    clip_dir = os.path.join(out_dir, "clip")
    os.makedirs(clip_dir, exist_ok=True)
    for old in glob.glob(os.path.join(clip_dir, "*")):
        os.remove(old)
    base = os.path.join(clip_dir, "clay_")
    with overrides() as o:
        _base_overrides(o, scene, cam, size, base)
        o.set(r, "engine", "BLENDER_WORKBENCH")
        _workbench_look(o, scene, look)
        o.set(scene, "frame_end", scene.frame_start + frames - 1)
        o.set(scene, "frame_step", 1)
        s = r.image_settings
        if hasattr(s, "media_type"):
            o.set(s, "media_type", "VIDEO")  # since Blender 5.0 the media type must be switched to video before FFMPEG can be chosen
        o.set(s, "file_format", "FFMPEG")
        ff = r.ffmpeg
        o.set(ff, "format", "MPEG4")
        o.set(ff, "codec", "H264")
        o.set(ff, "constant_rate_factor", "MEDIUM")
        o.set(ff, "ffmpeg_preset", "REALTIME")
        o.set(ff, "audio_codec", "NONE")
        o.set(ff, "gopsize", max(1, int(round(fps))))
        bpy.ops.render.render(animation=True)
    found = sorted(glob.glob(os.path.join(clip_dir, "*.mp4")), key=os.path.getmtime)
    if not found:
        raise RuntimeError("Blender did not write the preview video. Check that FFmpeg output is available in this Blender build.")
    return found[-1], frames / fps


def clip_seconds_for(spec, chosen: float) -> float:
    """Clay clip length: equals the output length and stays inside the range the model allows"""
    rules = spec.clip_rules
    lo = float(rules.get("min") or 2)
    hi = float(rules.get("max") or 15)
    return float(min(max(chosen, lo), hi))


def frames_note(scene, seconds: float) -> str:
    r = scene.render
    fps = r.fps / (r.fps_base or 1.0)
    n = max(2, int(round(seconds * fps)))
    return f"Frames {scene.frame_start}–{scene.frame_start + n - 1} ({seconds:g} s at {fps:g} fps)"


def last_render(path: str) -> str:
    """Save the "Render Result" (last F12) as PNG; raise a hint to render first when there is none"""
    rr = bpy.data.images.get("Render Result")
    if rr is None:
        raise RuntimeError("No render yet. Press F12 once, or switch the View to Camera.")
    try:
        rr.save_render(filepath=path)
    except RuntimeError as e:
        raise RuntimeError("No render yet. Press F12 once, or switch the View to Camera.") from e
    if not os.path.exists(path):
        raise RuntimeError("Could not save the last render.")
    return path


def export_image(img: bpy.types.Image, out_path_base: str) -> str:
    """Hand a reference image to the upload: use the original file when it exists on disk in a supported format, otherwise save a PNG (when too large, scale to 2048 and save JPEG)"""
    src = bpy.path.abspath(img.filepath) if img.filepath else ""
    ok_ext = (".png", ".jpg", ".jpeg", ".webp")
    if src and os.path.isfile(src) and src.lower().endswith(ok_ext) and os.path.getsize(src) <= 9 * 1024 * 1024 and not img.packed_file:
        return src
    copy = img.copy()
    try:
        w, h = copy.size
        big = max(w, h) > 2048
        if big:
            k = 2048 / max(w, h)
            copy.scale(max(1, int(w * k)), max(1, int(h * k)))
        ext = ".jpg" if big else ".png"
        path = out_path_base + ext
        copy.filepath_raw = path
        copy.file_format = "JPEG" if big else "PNG"
        copy.save()
        return path
    finally:
        bpy.data.images.remove(copy)


def aspect_of(size_wh: tuple[int, int]) -> float:
    w, h = size_wh
    return w / h if h else 1.0

