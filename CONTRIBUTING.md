# Contributing

Thanks for helping improve PopNow for Blender.

- **Bugs and ideas:** open an issue with the bug or feature template. For bugs, include your Blender version, OS, add-on version and the task ID from the Tasks panel (never your API key).
- **Pull requests:** keep them focused. Code, comments and UI text are English. Code in `popnow/core/` must not import `bpy`, so it stays testable with plain Python.
- **Tests:** run `python3 -m unittest discover -s tests -v` before you open a pull request. If you change capture or UI code, also run `blender --background --factory-startup --python tests/blender_selftest.py` (it uses a local fake backend, so it costs nothing).
- **Packaging:** `python3 scripts/build_zip.py --check` must pass.
- **License:** by contributing you agree that your contribution is licensed under GPL-3.0-or-later.

Questions about the PopNow API itself are best asked through [popnow.ai/contact](https://popnow.ai/contact).
