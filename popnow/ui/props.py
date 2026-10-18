"""Panel settings stored in the scene (prompt, model, tiers, reference images). Saved in the .blend, never contains the key.

Model-dependent dropdowns (aspect ratio, resolution, duration, quality) follow the selected model; options come from the bundled models.json.
Blender's dynamic enums require the returned strings to stay referenced, so they are cached per (model, option).
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from ..core import models, recipes

_ENUM_CACHE: dict[tuple, list] = {}


def _model_items(workflow: str):
    key = ("models", workflow)
    if key not in _ENUM_CACHE:
        _ENUM_CACHE[key] = [(m.model, m.label, m.note) for m in models.models_for(workflow)]
    return _ENUM_CACHE[key]


def _value_label(key: str, v) -> str:
    if key == "duration":
        return f"{v} s"
    if key == "aspect" and v in ("auto", "adaptive"):
        return "Auto"
    if isinstance(v, str):
        return v.upper() if v.endswith(("k", "p")) else v.replace("_", " ").capitalize()
    return str(v)


def _option_items(workflow_attr: str, key: str, match_label: str | None = None):
    """Build dropdown items for a model option; when match_label is set the first item follows the camera aspect ratio"""

    def items(self, context):
        spec = models.get(getattr(self, workflow_attr))
        vals = spec.option_values(key) if spec else []
        ck = (spec.model if spec else "", key, bool(match_label))
        if ck not in _ENUM_CACHE:
            out = []
            if match_label:
                out.append(("MATCH", match_label, "Pick the supported ratio closest to your camera or viewport"))
            for v in vals:
                if match_label and v in ("auto", "adaptive"):
                    continue
                out.append((str(v), _value_label(key, v), ""))
            if not out:
                out.append(("NONE", "—", "Not available for this model"))
            _ENUM_CACHE[ck] = out
        return _ENUM_CACHE[ck]

    return items


def option_value(spec: models.ModelSpec, key: str, ident: str):
    """Convert a dropdown string id back to the type the model expects (duration is an integer)"""
    for v in spec.option_values(key):
        if str(v) == ident:
            return v
    return None


def _reset_options(prefix: str, workflow_attr: str, keys: tuple[str, ...]):
    def update(self, context):
        spec = models.get(getattr(self, workflow_attr))
        if not spec:
            return
        for key in keys:
            attr = f"{prefix}_{key}"
            if not hasattr(self, attr):
                continue
            if key == "aspect":
                setattr(self, attr, "MATCH")
                continue
            d = spec.option_default(key)
            vals = [str(v) for v in spec.option_values(key)]
            ident = str(d) if d is not None and str(d) in vals else (vals[0] if vals else "NONE")
            try:
                setattr(self, attr, ident)
            except TypeError:
                pass

    return update


def _style_items(table):
    return [(k, v[0], v[1]) for k, v in table.items()]


_STYLE_ITEMS = _style_items(recipes.STYLES)
_TEX_STYLE_ITEMS = _style_items(recipes.TEXTURE_STYLES)
_PASS_ITEMS = [
    ("none", "None", "Only the clay render"),
    ("depth", "Depth", "Add a depth pass so the model keeps distances and shapes"),
    ("normal", "Normals", "Add a surface-normal pass"),
    ("lineart", "Line art", "Add outlines and creases taken from depth and normals"),
]
_LOOK_ITEMS = [
    ("CLAY", "Clay", "Uniform grey clay with cavity shading: the clearest input"),
    ("OBJECT", "Object colors", "One flat color per object (Object > Viewport Display > Color): good for telling figures apart"),
    ("MATERIAL", "Material colors", "Viewport colors of your materials"),
    ("TEXTURE", "Textures", "Image textures as the viewport shows them"),
]


def _image_poll(self, img) -> bool:
    return img.type in ("IMAGE", "UV_TEST") or bool(img.filepath)


class PopNowSceneProps(bpy.types.PropertyGroup):
    source: EnumProperty(
        name="View",
        items=[("CAMERA", "Camera", "Render from the scene camera"), ("VIEWPORT", "Viewport", "Render from the current 3D viewport angle")],
        default="CAMERA",
    )

    # ---------- Render with AI ----------
    render_source: EnumProperty(
        name="View",
        items=[
            ("CAMERA", "Camera", "Render a clay image from the scene camera"),
            ("VIEWPORT", "Viewport", "Render a clay image from the current 3D viewport angle"),
            ("RENDER", "Last render", "Use your last F12 render (your own lights and materials) instead of clay"),
        ],
        default="CAMERA",
    )
    render_prompt: StringProperty(name="Prompt", description="Describe the finished picture: subject, materials, setting, light", default="")
    render_style: EnumProperty(name="Style", items=_STYLE_ITEMS, default="photoreal")
    render_model: EnumProperty(name="Model", items=lambda s, c: _model_items("render"), update=_reset_options("render", "render_model", ("aspect", "resolution", "quality")))
    render_aspect: EnumProperty(name="Aspect", items=_option_items("render_model", "aspect", "Match camera"))
    render_resolution: EnumProperty(name="Resolution", items=_option_items("render_model", "resolution"))
    render_quality: EnumProperty(name="Quality", items=_option_items("render_model", "quality"))
    render_pass: EnumProperty(name="Structure", description="Extra pass that helps the model keep your shapes", items=_PASS_ITEMS, default="none")
    render_look: EnumProperty(name="Clay look", items=_LOOK_ITEMS, default="CLAY")
    control_strength: FloatProperty(name="Follow shapes", description="How strictly the result follows the structure pass", default=0.7, min=0.0, max=1.0)
    ref_1: PointerProperty(name="Reference 1", type=bpy.types.Image, poll=_image_poll, description="A character or product photo the result should match")
    ref_2: PointerProperty(name="Reference 2", type=bpy.types.Image, poll=_image_poll)
    ref_3: PointerProperty(name="Reference 3", type=bpy.types.Image, poll=_image_poll)

    # ---------- Animate with AI ----------
    anim_prompt: StringProperty(name="Prompt", description="Describe who is in the shot, what they wear, the place and the light", default="")
    anim_style: EnumProperty(name="Style", items=_STYLE_ITEMS, default="cinematic")
    anim_model: EnumProperty(name="Model", items=lambda s, c: _model_items("animate"), update=_reset_options("anim", "anim_model", ("aspect", "resolution", "duration", "animation_mode")))
    anim_aspect: EnumProperty(name="Aspect", items=_option_items("anim_model", "aspect", "Match camera"))
    anim_resolution: EnumProperty(name="Resolution", items=_option_items("anim_model", "resolution"))
    anim_duration: EnumProperty(name="Length", items=_option_items("anim_model", "duration"))
    anim_animation_mode: EnumProperty(name="Mode", items=_option_items("anim_model", "animation_mode"))
    anim_clip_seconds: IntProperty(name="Length", description="Seconds of your animation to send (from the scene start frame)", default=5, min=2, max=30)
    anim_audio: BoolProperty(name="Generate sound", description="Ask the model for matching sound (some models charge more)", default=False)
    anim_look: EnumProperty(name="Clay look", items=_LOOK_ITEMS, default="OBJECT")
    anim_ref_1: PointerProperty(name="Reference 1", type=bpy.types.Image, poll=_image_poll)
    anim_ref_2: PointerProperty(name="Reference 2", type=bpy.types.Image, poll=_image_poll)
    anim_character: PointerProperty(name="Character", type=bpy.types.Image, poll=_image_poll, description="Who should perform the motion (needed by motion-control models)")

    # ---------- Texture ----------
    tex_prompt: StringProperty(name="Prompt", description="The material, for example: mossy cobblestone, worn oak planks", default="")
    tex_style: EnumProperty(name="Style", items=_TEX_STYLE_ITEMS, default="scan")
    tex_model: EnumProperty(name="Model", items=lambda s, c: _model_items("texture"), update=_reset_options("tex", "tex_model", ("resolution",)))
    tex_resolution: EnumProperty(name="Resolution", items=_option_items("tex_model", "resolution"))
    tex_seamless: BoolProperty(name="Make seamless", description="Blend the edges so the texture tiles without visible seams", default=True)
    tex_apply: BoolProperty(name="Apply to selected objects", description="Create a material with the texture and assign it to the selected objects", default=True)
    tex_scale: FloatProperty(name="Tile scale", description="How many times the texture repeats", default=1.0, min=0.01, max=100.0)

    # ---------- task list ----------
    show_all_history: BoolProperty(name="Show all", default=False)
    # Dynamic dropdowns have no per-model default, so fill them from the model defaults on first use (init_defaults)
    defaults_set: BoolProperty(default=False, options={"HIDDEN"})


def init_defaults(scene) -> None:
    """Fill each dropdown with the selected model's defaults (new scenes, or old scenes the first time the add-on is installed)"""
    p = getattr(scene, "popnow", None)
    if p is None or p.defaults_set:
        return
    _reset_options("render", "render_model", ("aspect", "resolution", "quality"))(p, None)
    _reset_options("anim", "anim_model", ("aspect", "resolution", "duration", "animation_mode"))(p, None)
    _reset_options("tex", "tex_model", ("resolution",))(p, None)
    p.defaults_set = True


def _init_all_scenes():
    try:
        for sc in bpy.data.scenes:
            init_defaults(sc)
    except Exception as e:  # data cannot be written in a restricted context; try again next time
        print(f"PopNow: could not set defaults yet: {e}")
    return None


@bpy.app.handlers.persistent
def _on_load(_dummy=None) -> None:
    _init_all_scenes()


CLASSES = (PopNowSceneProps,)


def register() -> None:
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.popnow = PointerProperty(type=PopNowSceneProps)
    bpy.app.handlers.load_post.append(_on_load)
    # the context is restricted during registration and cannot write data, so defer to the next tick
    bpy.app.timers.register(_init_all_scenes, first_interval=0.5)


def unregister() -> None:
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    del bpy.types.Scene.popnow
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
