"""Release guards: the bundled model table is well formed, the zip builds cleanly, and the manifest follows the extension rules."""

import json
import os
import subprocess
import sys
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(ROOT, "popnow")
WORKFLOWS = {"render", "animate", "texture"}


def run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", script), "--check"], capture_output=True, text=True, cwd=ROOT)


class ReleaseTests(unittest.TestCase):
    def test_models_json_is_well_formed(self):
        with open(os.path.join(SRC, "core", "models.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["models"])
        seen = set()
        for m in data["models"]:
            self.assertNotIn(m["model"], seen, m["model"])
            seen.add(m["model"])
            self.assertEqual(len(m["model"].split("/")), 3, m["model"])
            self.assertIn(m["workflow"], WORKFLOWS, m["model"])
            self.assertTrue(m["label"], m["model"])
            self.assertIn("credits", m)
            for media in m["media"].values():
                self.assertIn("field", media)
        self.assertEqual({m["workflow"] for m in data["models"]}, WORKFLOWS)

    def test_zip_builds_cleanly(self):
        r = run("build_zip.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_manifest_follows_extension_rules(self):
        with open(os.path.join(SRC, "blender_manifest.toml"), "rb") as f:
            m = tomllib.load(f)
        for key in ("schema_version", "id", "version", "name", "tagline", "maintainer", "type", "blender_version_min", "license"):
            self.assertTrue(m.get(key), key)
        # Platform rules: tagline at most 64 characters and not ending in punctuation; permission reasons at most 64 characters, no trailing period
        self.assertLessEqual(len(m["tagline"]), 64)
        self.assertNotIn(m["tagline"][-1], ".!?,;:")
        for k, why in m["permissions"].items():
            self.assertLessEqual(len(why), 64, k)
            self.assertFalse(why.endswith("."), k)
        self.assertGreaterEqual(tuple(int(x) for x in m["blender_version_min"].split(".")), (4, 2, 0))

    def test_ui_strings_are_english(self):
        # UI text is English only: string literals must not contain CJK characters
        for dirpath, _, files in os.walk(SRC):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    text = f.read()
                for lit in _string_literals(text):
                    self.assertFalse(any(_is_cjk(ch) for ch in lit), f"{name}: CJK text in a string: {lit[:60]}")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF


def _string_literals(source: str):
    """String constants in the source, excluding docstrings"""
    import ast

    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value


if __name__ == "__main__":
    unittest.main()
