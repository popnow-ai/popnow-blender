"""Add-on preferences: PopNow account sign-in, API key, server URLs, output folder, confirmation thresholds.

Two ways to pay: sign in with a PopNow account (credits, the same balance as on popnow.ai), or a developer API key
(the same balance, shown in USD). With a key set, the add-on uses the key.
The key is stored only in Blender's user preferences (userpref.blend), never in a .blend project file; the field is a password field.
The account session is a private file in the add-on's own folder (runtime.account_path), never in userpref.blend.
"""

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty

from . import runtime
from ..core.account import DEFAULT_MCP_URL, PRICING_URL
from ..core.client import BILLING_URL, CONSOLE_URL, DEFAULT_BASE_URL, mask_key


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
    mcp_url: StringProperty(
        name="Account server URL",
        description="Change only for local testing",
        default=DEFAULT_MCP_URL,
    )
    auto_confirm_credits: IntProperty(
        name="Start without asking up to (credits)",
        description="Signed in: skip the confirm step when the price is at most this many credits. 0 always asks",
        default=0,
        min=0,
        max=5000,
    )
    auto_confirm_usd: FloatProperty(
        name="Start without asking up to ($)",
        description="With an API key: skip the confirm step when the quote holds at most this many US dollars. 0 always asks",
        default=0.0,
        min=0.0,
        max=5.0,
        step=1,
        precision=2,
    )
    # Deprecated: the 0.1.x threshold in credits (1 credit = $0.001). Not shown; moved to auto_confirm_usd once (runtime.auto_confirm_usd)
    auto_confirm_below: IntProperty(
        name="Start without asking up to (credits, deprecated)",
        description="Deprecated: use the USD setting",
        default=0,
        min=0,
        max=5000,
        options={"HIDDEN"},
    )
    open_results: BoolProperty(
        name="Show image results in an Image Editor",
        description="Open finished renders in an Image Editor (a new window if none is open)",
        default=True,
    )

    def draw(self, context) -> None:
        layout = self.layout
        col = layout.column()
        if not runtime.online_allowed():
            box = col.box()
            box.label(text="Online access is off in Preferences > System. PopNow needs it to work.", icon="ERROR")
        mode = runtime.mode()

        box = col.box()
        box.label(text="PopNow account", icon="USER")
        login = runtime.LOGIN
        if runtime.signed_in():
            row = box.row(align=True)
            row.label(text="Signed in to PopNow", icon="CHECKMARK")
            row.operator("popnow.sign_out", icon="UNLINKED")
            if mode == "api":
                box.label(text="An API key is set below, so the add-on uses the key. Clear it to pay with your credits.", icon="INFO")
        elif login.get("state") == "waiting":
            row = box.row(align=True)
            row.label(text="Finish signing in in your browser…", icon="TIME")
            row.operator("popnow.sign_in_cancel", text="Cancel")
            if login.get("url"):
                box.operator("popnow.copy_sign_in_link", icon="COPYDOWN")
        else:
            row = box.row()
            row.scale_y = 1.3
            row.operator("popnow.sign_in", icon="URL")
            if login.get("state") == "error":
                sub = box.row()
                sub.alert = True
                sub.label(text=str(login.get("error") or "")[:110], icon="ERROR")
        row = box.row(align=True)
        row.prop(self, "auto_confirm_credits")
        row.operator("wm.url_open", text="Get credits", icon="URL").url = PRICING_URL

        box = col.box()
        box.label(text="Developer API", icon="KEYINGSET")
        row = box.row(align=True)
        row.prop(self, "api_key")
        row.operator("wm.url_open", text="Get API key", icon="URL").url = CONSOLE_URL
        if self.api_key:
            # USD details only once a key is set: signed in without a key, the add-on shows credits only
            row.operator("wm.url_open", text="Add funds", icon="URL").url = BILLING_URL
            status = runtime.key_status()
            if status == "malformed":
                box.label(text="That does not look like a PopNow key (sk-popnow-…).", icon="ERROR")
            else:
                box.label(text=f"Using {mask_key(self.api_key)}", icon="CHECKMARK")
            box.prop(self, "auto_confirm_usd")
            box.label(text="Your PopNow balance, shown in USD (the same balance as on popnow.ai).", icon="INFO")

        col.separator()
        col.prop(self, "output_dir")
        col.prop(self, "open_results")
        col.separator()
        sub = col.column()
        sub.prop(self, "mcp_url")
        sub.prop(self, "base_url")
