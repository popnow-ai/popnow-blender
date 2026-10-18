"""Add-on preferences: API key, API base URL, output folder, confirmation threshold.

The key is stored only in Blender's user preferences (userpref.blend), never in a .blend project file; the field is a password field.
"""

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from . import runtime
from ..core.client import CONSOLE_URL, DEFAULT_BASE_URL, mask_key


def _key_changed(self, context) -> None:
    # strip whitespace picked up when pasting, then fetch the balance right away to verify
    k = self.api_key.strip()
    if k != self.api_key:
        self.api_key = k
        return
    runtime.BALANCE.clear()
    runtime.refresh_balance()


class POPNOW_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = runtime.ADDON_ID

    api_key: StringProperty(
        name="API key",
        description="Your PopNow API key (starts with sk-popnow-). Stored in your Blender preferences, never in .blend files",
        subtype="PASSWORD",
        update=_key_changed,
    )
    base_url: StringProperty(
        name="API base URL",
        description="Change only for local testing",
        default=DEFAULT_BASE_URL,
    )
    output_dir: StringProperty(
        name="Save results to",
        description="Folder for downloaded results. Empty: a popnow folder next to the .blend file (or the add-on folder for unsaved files)",
        subtype="DIR_PATH",
    )
    auto_confirm_below: IntProperty(
        name="Start without asking up to",
        description="Skip the confirm step when the quote is at most this many credits. 0 always asks",
        default=0,
        min=0,
        max=500,
    )
    open_results: BoolProperty(
        name="Show image results in an Image Editor",
        description="Open finished renders in an Image Editor (a new window if none is open)",
        default=True,
    )

    def draw(self, context) -> None:
        layout = self.layout
        col = layout.column()
        row = col.row(align=True)
        row.prop(self, "api_key")
        row.operator("wm.url_open", text="Get API key", icon="URL").url = CONSOLE_URL
        if self.api_key:
            status = runtime.key_status()
            if status == "malformed":
                col.label(text="That does not look like a PopNow key (sk-popnow-…).", icon="ERROR")
            else:
                col.label(text=f"Using {mask_key(self.api_key)}", icon="CHECKMARK")
        if not runtime.online_allowed():
            box = col.box()
            box.label(text="Online access is off in Preferences > System. PopNow needs it to work.", icon="ERROR")
        col.separator()
        col.prop(self, "output_dir")
        col.prop(self, "auto_confirm_below")
        col.prop(self, "open_results")
        col.separator()
        sub = col.column()
        sub.prop(self, "base_url")
        sub.label(text="Results, quotes and charges use the same credits as popnow.ai.", icon="INFO")
