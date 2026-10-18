"""PopNow for Blender: turn clay renders and camera animation into AI images, video and textures with PopNow.

Layout: core/ does not depend on bpy (requests, polling, pricing, prompts, image processing; pure Python unit tests); ui/ is the Blender side.
All user-facing text is English.
"""

try:
    import bpy
except ImportError:  # outside Blender (pure Python unit tests) only core is needed
    bpy = None

if bpy is not None:
    from .ui import ops, panels, prefs, props, results, runtime

    _CLASSES = (prefs.POPNOW_AP_preferences, *ops.CLASSES, *panels.CLASSES)


def register() -> None:
    props.register()
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    runtime.set_apply_handler(results.apply)
    runtime.load_history()
    runtime.ensure_timer()


def unregister() -> None:
    runtime.shutdown()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    props.unregister()
