"""Offline public documentation checks; no application imports or network."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

MIT = '''MIT License

Copyright (c) 2026 JeisonOJ

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

INTENDED_USE = '''### Intended Use and Copyright

JAnimeVault is a local anime library organizer designed to help users catalog and manage their personal collections.

JAnimeVault does not download, distribute, or provide access to copyrighted anime episodes or other media files.

Users are responsible for ensuring that the content they manage complies with applicable copyright laws.

JAnimeVault is an independent hobby project and is not affiliated with or endorsed by anime studios, publishers, or distributors.'''


class PublicDocumentTests(unittest.TestCase):
    def test_complete_mit_and_disclosures(self):
        self.assertEqual((ROOT / "LICENSE").read_text(encoding="utf-8"), MIT)
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(INTENDED_USE, text)
        self.assertIn("## Development and AI Assistance", text)
        self.assertIn("Some visual assets were created with AI assistance.", text)
        self.assertIn("Copyright (c) 2026 JeisonOJ", text)

    def test_links_and_source_completeness(self):
        paths = [ROOT / "README.md", ROOT / "THIRD_PARTY_NOTICES.md"]
        paths += list((ROOT / "docs").rglob("*.md"))
        paths += [ROOT / "licenses/README.md", ROOT / "prototypes/native_messaging/README.md"]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                self.assertTrue((path.parent / target.split("#")[0]).exists(), (path, target))
            for target in re.findall(r'<img[^>]+src="([^"]+)"', text):
                self.assertTrue((path.parent / target).is_file())
        for path in ("main.py", "indexer/database.py", "indexer/service.py", "indexer/covers.py",
                     "indexer/capture_format.py", "ui/controller.py", "ui/qml/Main.qml",
                     "ui/qml/PlannedView.qml", "ui/qml/PlannedSearchDialog.qml",
                     "prototypes/native_messaging/host.py", "prototypes/native_messaging/prepare_host.py"):
            self.assertTrue((ROOT / path).is_file())
        self.assertIn("PySide6==6.11.1", (ROOT / "requirements.txt").read_text())
        self.assertTrue((ROOT / "docs/screenshots/library.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_source_setup_matches_parser_without_running_setup(self):
        import ast
        prepare = ROOT / "prototypes/native_messaging/prepare_host.py"
        options = {n.value for n in ast.walk(ast.parse(prepare.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("--")}
        guide = (ROOT / "docs/browser_extension_setup.md").read_text(encoding="utf-8")
        self.assertLessEqual(set(re.findall(r"--[a-z][a-z-]*", guide)), options)
        for name in ("edge", "chrome"):
            self.assertIn(f"--{name}-id YOUR_{name.upper()}_EXTENSION_ID --inbox production", guide)
        self.assertIn("does **not** change the registry automatically", guide)
        self.assertIn("do **not** import them blindly", guide)
        self.assertIn("IDs can differ", guide)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python -m venv .venv", readme)
        self.assertIn("pip install -r", readme)
        self.assertIn("python.exe .\\main.py", readme)

    def test_only_source_assets_and_applicable_license_references(self):
        self.assertTrue((ROOT / "licenses/LGPL-3.0.txt").is_file())
        self.assertTrue((ROOT / "licenses/GPL-3.0.txt").is_file())
        self.assertFalse(any((ROOT / "packaging").rglob("*.*")))
        self.assertFalse((ROOT / "licenses/qt").exists() and any((ROOT / "licenses/qt").iterdir()))
        for path in [ROOT/"README.md", *list((ROOT/"docs").rglob("*.md"))]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PyInstaller", text)
            self.assertNotIn("JAnimeVault.exe", text)
            self.assertNotIn("JAnimeVaultNativeHost.exe", text)
            self.assertNotIn("source_correspondence", text)
        self.assertIn("API access does not grant permission", (ROOT/"THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
