"""Turn panel settings into one createTask input: how the prompt is written and which field each input goes in.

The prompt wording follows what was validated for clay-to-final workflows:
say what to take from the clay scene (composition, camera angle, camera move, blocking, timing) and what not to take (grey material, proxy geometry, flat light);
reference images use the model's own naming (Seedance writes @Image1, Wan writes Image 1, Kling writes <<<image_1>>>).
Text written by the add-on stays neutral; the user's own prompt goes in as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ModelSpec

# Style presets: id -> (UI name, sentence added to the prompt)
STYLES: dict[str, tuple[str, str]] = {
    "none": ("None", ""),
    "photoreal": ("Photoreal", "Photorealistic, natural light, real-world materials, sharp detail, shot on a full-frame camera."),
    "cinematic": ("Cinematic", "Cinematic film still, motivated lighting, shallow depth of field, rich color grade."),
    "product": ("Product shot", "Clean studio product photography, soft-box lighting, seamless backdrop, crisp reflections."),
    "archviz": ("Architecture", "Architectural visualization, photoreal materials, soft daylight, straight verticals."),
    "stylized": ("Stylized 3D", "Stylized 3D animated feature look, soft global illumination, appealing shapes and colors."),
    "anime": ("Anime", "Anime illustration, clean line art, cel shading, vibrant colors."),
    "concept": ("Concept art", "Painterly concept art, expressive brushwork, atmospheric light."),
    "watercolor": ("Watercolor", "Loose watercolor illustration on textured paper, soft washes of color."),
}

TEXTURE_STYLES: dict[str, tuple[str, str]] = {
    "scan": ("Photo scan", "Photoscanned PBR material, realistic micro detail, neutral color, no baked lighting."),
    "painted": ("Hand-painted", "Hand-painted stylized game texture, soft brush strokes, readable shapes."),
    "toon": ("Toon", "Flat toon-shaded texture, bold simple shapes, limited palette."),
    "none": ("None", ""),
}

# Structure reference passes: id -> (UI name, explanation for edit models)
PASSES: dict[str, tuple[str, str]] = {
    "none": ("None", ""),
    "depth": ("Depth", "a depth map of the same view (near is white, far is black)"),
    "normal": ("Normals", "a surface-normal map of the same view"),
    "lineart": ("Line art", "a line drawing of the same view"),
}


@dataclass
class Uploaded:
    """Uploaded inputs (popnow://f/...) grouped by purpose"""

    structure: list[str] = field(default_factory=list)  # [clay render, structure pass?]
    control: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    clip: list[str] = field(default_factory=list)
    character: list[str] = field(default_factory=list)


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _style(style: str, table: dict[str, tuple[str, str]] = STYLES) -> str:
    return table.get(style, ("", ""))[1]


def render_prompt(spec: ModelSpec, user_prompt: str, style: str, *, pass_kind: str, n_refs: int, source: str = "clay") -> str:
    """Prompt for Render with AI"""
    user = (user_prompt or "").strip()
    if spec.media("control"):
        # ControlNet: the control image only drives layout; the prompt describes the picture itself
        return _join(user or "A finished, detailed scene.", _style(style))
    st = spec.media("structure")
    field_name = st["field"] if st else "image_urls"
    img1 = spec.token(field_name, 1) or "the first image"
    if source == "render":
        # The user's own F12 render: keep its light and materials, only add detail towards a finished image
        lines = [
            f"{img1} is a 3D render of a scene.",
            f"Turn it into a finished image: keep the exact composition, camera angle, object placement, proportions, colors and lighting direction from {img1}, "
            "and add realistic detail, texture and lighting quality.",
        ]
    else:
        lines = [
            f"{img1} is a grey clay render of a 3D scene.",
            f"Turn it into a finished image: keep the exact composition, camera angle, object placement, proportions and silhouettes from {img1}, "
            "and replace the grey clay surfaces with real materials, lighting and detail.",
        ]
    idx = 2
    if pass_kind != "none" and PASSES.get(pass_kind, ("", ""))[1]:
        tok = spec.token(field_name, idx) or f"image {idx}"
        lines.append(f"{tok} is {PASSES[pass_kind][1]}; follow it for shape and distance only.")
        idx += 1
    if user:
        lines.append(f"Scene: {user}")
    if n_refs:
        refs_field = spec.media("refs")["field"] if spec.media("refs") else field_name
        toks = [spec.token(refs_field, idx + i) or f"image {idx + i}" for i in range(n_refs)]
        lines.append(f"Use {_list(toks)} as the reference for how the main subject looks.")
    lines.append(_style(style))
    return _join(*lines)


def animate_prompt(spec: ModelSpec, user_prompt: str, style: str, *, n_refs: int) -> str:
    """Prompt for Animate with AI (one wording each for reference-to-video, video restyle and motion transfer)"""
    user = (user_prompt or "").strip()
    task = spec.raw.get("task")
    refs = spec.media("refs")
    ref_toks = [spec.token(refs["field"], i + 1) or f"reference image {i + 1}" for i in range(n_refs)] if refs and n_refs else []
    if task == "character-animation":
        # Motion transfer: motion comes from the clay clip, appearance from the character image; the prompt is optional
        return _join(user, _style(style))
    if task == "video-edit":
        lines = [
            "Restyle this grey clay previz video into a finished shot.",
            f"Scene: {user}" if user else "",
            "Keep every camera move, position, timing and motion; replace the grey placeholder materials and proxy shapes with real ones.",
            _style(style),
        ]
        if ref_toks:
            lines.append(f"{_list(ref_toks)} shows how the main subject looks.")
        return _join(*lines)
    clip = spec.media("clip")
    v1 = spec.token(clip["field"], 1) if clip else None
    v1 = v1 or "the reference video"
    lines = [
        f"{v1} is a grey clay previz. Copy its camera movement, framing, blocking, timing and poses exactly.",
        f"The grey figures and shapes are placeholders: replace them with the real subjects and world described here, and inherit nothing else from {v1} "
        "(no grey materials, proxy geometry or flat lighting).",
        f"Scene: {user}" if user else "",
        _style(style),
    ]
    if ref_toks:
        lines.append(f"{_list(ref_toks)} shows how the main subject looks.")
    return _join(*lines)


def texture_prompt(user_prompt: str, style: str) -> str:
    user = (user_prompt or "").strip() or "weathered stone"
    return _join(
        f"Seamless tileable texture of {user}.",
        "Flat top-down orthographic view that fills the whole frame, even diffuse lighting, no shadows, no perspective, no borders, no text.",
        _style(style, TEXTURE_STYLES),
    )


def _list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_input(spec: ModelSpec, prompt: str, options: dict[str, Any], media: Uploaded) -> dict:
    """Build input: only the options and inputs the user can change; every other field uses the server default"""
    out: dict[str, Any] = {}
    if prompt:
        limit = spec.raw.get("promptMax")
        out["prompt"] = prompt[: int(limit)] if limit else prompt
    for key, value in options.items():
        opt = spec.option(key)
        if opt is None or value is None:
            continue
        out[opt["field"]] = value

    def put(slot: str, uris: list[str]) -> None:
        m = spec.media(slot)
        if not m or not uris:
            return
        if m["array"]:
            cur = out.setdefault(m["field"], [])
            room = m["max"] - len(cur)
            cur.extend(uris[: max(0, room)])
        else:
            out[m["field"]] = uris[0]

    put("structure", media.structure)
    put("control", media.control)
    put("refs", media.refs)
    put("clip", media.clip)
    put("character", media.character)
    return out


def missing_inputs(spec: ModelSpec, media: Uploaded, prompt: str) -> list[str]:
    """Check required inputs before submitting; returns UI hints (English)"""
    problems = []
    if "prompt" in spec.raw.get("required", []) and not prompt.strip():
        problems.append("Write a prompt.")
    if spec.media("character") and not media.character:
        problems.append("Pick a character image (the person or creature that should copy the motion).")
    return problems
