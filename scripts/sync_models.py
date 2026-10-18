#!/usr/bin/env python3
"""Regenerate the bundled model table popnow/core/models.json from the PopNow model catalog.

Source: the live catalog at GET https://api.popnow.ai/api/v1/models?includeSchema=1 (needs POPNOW_API_KEY),
or a saved copy of that response passed with --source FILE.
The add-on only ships the few endpoints its three workflows (clay render, clay animation, texture) use. For each one it
keeps only the options the UI shows, where each input goes, how the prompt names an input, and the credit price table
(for local estimates only; the amount actually held always comes from jobs/quote).
Fields the catalog response does not carry (for example typicalSeconds or the pricing mode) are kept from the
current models.json.

Usage (from the repository root):
  POPNOW_API_KEY=sk-popnow-... python3 scripts/sync_models.py [--check]
  python3 scripts/sync_models.py --source models-response.json [--check]
--check: compare only, write nothing; exits 1 when models.json is out of date.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "popnow/core/models.json"
API_BASE = os.environ.get("POPNOW_API_BASE", "https://api.popnow.ai/api/v1")

# Which endpoints each workflow uses, what the UI calls them, and which field each input goes in.
# media keys:
#   structure  array field that takes the clay render (Render), placed first
#   control    single field that takes the structure control image (depth / line art / normal; ControlNet models only)
#   refs       array field for reference images (character / product), placed after the clay render
#   clip       field for the clay video (array or single)
#   character  character image for motion transfer (single field, required)
RECIPES: list[dict] = [
    # ---- Render with AI: clay render + reference images -> image ----
    {"workflow": "render", "model": "bytedance/seedream-5.0-pro/edit", "label": "Seedream 5.0 Pro",
     "note": "Balanced. Follows your layout closely", "media": {"structure": "image_urls", "refs": "image_urls"}},
    {"workflow": "render", "model": "bytedance/seedream-5.0-flash/edit", "label": "Seedream 5.0 Flash",
     "note": "Faster and cheaper", "media": {"structure": "image_urls", "refs": "image_urls"}},
    {"workflow": "render", "model": "google/nano-banana-2.1/edit", "label": "Nano Banana 2.1",
     "note": "Best with many reference images", "media": {"structure": "image_urls", "refs": "image_urls"}},
    {"workflow": "render", "model": "openai/gpt-image-2/edit", "label": "GPT Image 2",
     "note": "Strong text and fine detail", "media": {"structure": "image_urls", "refs": "image_urls"}},
    {"workflow": "render", "model": "alibaba/qwen-image-2.1-turbo/edit", "label": "Qwen-Image 2.1 Turbo",
     "note": "Cheapest drafts", "media": {"structure": "image_urls", "refs": "image_urls"}},
    {"workflow": "render", "model": "black-forest-labs/flux-1-dev-controlnet-lora/text-to-image", "label": "FLUX.1 dev ControlNet",
     "note": "Exact shapes from a depth or line pass. No reference images", "media": {"control": "control_image_url"}},
    # ---- Animate with AI: clay video (camera animation) -> video ----
    {"workflow": "animate", "model": "bytedance/seedance-2.0-mini/reference-to-video", "label": "Seedance 2.0 Mini",
     "note": "Cheapest. Good for drafts", "media": {"clip": "reference_video_urls", "refs": "reference_image_urls"}},
    {"workflow": "animate", "model": "bytedance/seedance-2.0-fast/reference-to-video", "label": "Seedance 2.0 Fast",
     "note": "Good balance of price and quality", "media": {"clip": "reference_video_urls", "refs": "reference_image_urls"}},
    {"workflow": "animate", "model": "bytedance/seedance-2.0/reference-to-video", "label": "Seedance 2.0",
     "note": "Higher quality, up to 4K", "media": {"clip": "reference_video_urls", "refs": "reference_image_urls"}},
    {"workflow": "animate", "model": "bytedance/seedance-2.5/reference-to-video", "label": "Seedance 2.5",
     "note": "Follows clay previz best", "media": {"clip": "reference_video_urls", "refs": "reference_image_urls"}},
    {"workflow": "animate", "model": "alibaba/wan-3.0/reference-to-video", "label": "Wan 3.0",
     "note": "Strong motion, up to 1080p", "media": {"clip": "reference_video_urls", "refs": "reference_image_urls"}},
    {"workflow": "animate", "model": "kling/o3/video-edit", "label": "Kling O3 Restyle",
     "note": "Repaints your clip frame by frame. 3 to 10 s", "media": {"clip": "video_url", "refs": "reference_image_urls"},
     "clip": {"min": 3, "max": 10, "minSide": 720}},
    {"workflow": "animate", "model": "alibaba/wan-2.2-animate/character-animation", "label": "Wan 2.2 Animate",
     "note": "Motion control: your character copies the clay figure", "media": {"clip": "video_url", "character": "image_url"},
     "clip": {"min": 2, "max": 30}},
    # ---- Texture: prompt -> square texture (made seamless locally by the add-on) ----
    {"workflow": "texture", "model": "bytedance/seedream-5.0-flash/text-to-image", "label": "Seedream 5.0 Flash", "note": "Fast and cheap", "media": {}},
    {"workflow": "texture", "model": "alibaba/qwen-image-2.1-turbo/text-to-image", "label": "Qwen-Image 2.1 Turbo", "note": "Cheapest", "media": {}},
    {"workflow": "texture", "model": "bytedance/seedream-5.0-pro/text-to-image", "label": "Seedream 5.0 Pro", "note": "Most detail", "media": {}},
    {"workflow": "texture", "model": "google/nano-banana-2.1/text-to-image", "label": "Nano Banana 2.1", "note": "Up to 4K", "media": {}},
]

# Options the UI exposes directly (field name -> name inside the add-on); every other field uses the server default
OPTION_FIELDS = {
    "aspect_ratio": "aspect",
    "resolution": "resolution",
    "duration_seconds": "duration",
    "quality": "quality",
    "generate_audio": "audio",
    "control_strength": "control_strength",
    "animation_mode": "animation_mode",
}


def _media_entry(schema: dict, field: str) -> dict:
    """Input field: whether it is an array and how many items it takes (a single field counts as 1)"""
    prop = schema["properties"].get(field) or {}
    if prop.get("type") == "array":
        return {"field": field, "array": True, "max": int(prop.get("maxItems") or 1)}
    return {"field": field, "array": False, "max": 1}


def _fetch_catalog() -> dict:
    key = os.environ.get("POPNOW_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set POPNOW_API_KEY, or pass --source with a saved GET /api/v1/models?includeSchema=1 response")
    req = urllib.request.Request(f"{API_BASE}/models?includeSchema=1", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read().decode("utf-8"))


def _endpoints(catalog: dict) -> list[dict]:
    """Accept the enveloped API response, its data object, or a bare list of models"""
    if isinstance(catalog, list):
        return catalog
    data = catalog.get("data", catalog)
    return data.get("items") or data.get("endpoints") or []


def build(catalog: dict, previous: dict) -> dict:
    by_model = {e["model"]: e for e in _endpoints(catalog)}
    prev = {m["model"]: m for m in previous.get("models", [])}
    out = []
    for r in RECIPES:
        e = by_model.get(r["model"])
        if e is None:
            raise SystemExit(f"{r['model']} is not in the catalog (retired or renamed? update RECIPES first)")
        schema = e.get("inputSchema")
        if not schema:
            raise SystemExit(f"{r['model']} has no inputSchema (request the catalog with includeSchema=1)")
        props = schema["properties"]
        for field in r["media"].values():
            if field not in props:
                raise SystemExit(f"{r['model']} has no field {field}")
        options = {}
        for field, key in OPTION_FIELDS.items():
            p = props.get(field)
            if not p:
                continue
            opt: dict = {"field": field}
            if "enum" in p:
                opt["values"] = p["enum"]
            elif p.get("type") == "boolean":
                opt["values"] = [False, True]
            elif p.get("type") in ("number", "integer"):
                opt["min"] = p.get("minimum")
                opt["max"] = p.get("maximum")
            else:
                # integer durations without an enum (Kling O3 and similar) are not exposed in the UI
                continue
            if "default" in p:
                opt["default"] = p["default"]
            options[key] = opt
        tokens = (props.get("prompt", {}).get("x-ui", {}) or {}).get("reference_tokens") or {}
        media = {k: _media_entry(schema, f) for k, f in r["media"].items()}
        old = prev.get(r["model"], {})
        task = e.get("task") or (e.get("tasks") or [None])[0] or old.get("task")
        out.append({
            "model": e["model"],
            "workflow": r["workflow"],
            "label": r["label"],
            "note": r["note"],
            "provider": e.get("provider") or old.get("provider"),
            "task": task,
            "typicalSeconds": e.get("typicalSeconds", old.get("typicalSeconds")),
            "required": schema.get("required") or [],
            "options": options,
            "media": media,
            "tokens": tokens,
            "promptMax": props.get("prompt", {}).get("maxLength"),
            "clip": r.get("clip"),
            "credits": e["credits"],
            "pricing": schema.get("x-pricing") or old.get("pricing") or {},
        })
    snapshot = catalog.get("snapshot") if isinstance(catalog, dict) else None
    return {
        "version": 1,
        "snapshot": snapshot or previous.get("snapshot") or datetime.date.today().isoformat(),
        "source": "Generated by scripts/sync_models.py from the PopNow model catalog (GET /api/v1/models); do not edit by hand",
        "models": out,
    }


def render(catalog: dict, previous: dict) -> str:
    return json.dumps(build(catalog, previous), ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    if "--source" in argv:
        catalog = json.loads(Path(argv[argv.index("--source") + 1]).read_text("utf-8"))
    else:
        catalog = _fetch_catalog()
    current = TARGET.read_text("utf-8") if TARGET.exists() else ""
    previous = json.loads(current) if current else {}
    text = render(catalog, previous)
    if "--check" in argv:
        if current != text:
            print("popnow/core/models.json is out of date: run python3 scripts/sync_models.py")
            return 1
        print("models.json is up to date")
        return 0
    TARGET.write_text(text, "utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(json.loads(text)['models'])} endpoints)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
