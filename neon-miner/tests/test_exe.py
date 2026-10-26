"""Проверки готового Windows-артефакта (сеть и Windows не нужны)."""

from __future__ import annotations

import hashlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIST = ROOT / "dist"
EXE = DIST / "NeonMiner.exe"


@unittest.skipUnless(EXE.exists(), "dist/NeonMiner.exe не собран — запустите windows/build_windows.py")
class WindowsExeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = EXE.read_bytes()

    def test_valid_pe64(self):
        self.assertEqual(self.data[:2], b"MZ", "нет DOS-заголовка MZ")
        off = int.from_bytes(self.data[0x3c:0x40], "little")
        self.assertEqual(self.data[off:off + 4], b"PE\0\0", "нет PE-подписи")
        machine = int.from_bytes(self.data[off + 4:off + 6], "little")
        self.assertEqual(machine, 0x8664, "не x86-64")
        subsystem = int.from_bytes(self.data[off + 0x5c:off + 0x5e], "little")
        self.assertEqual(subsystem, 3, "ожидался консольный subsystem")
        self.assertGreater(len(self.data), 100_000)
        self.assertLess(len(self.data), 2_000_000, "exe подозрительно большой")

    def test_sha256sums_matches(self):
        digest = hashlib.sha256(self.data).hexdigest()
        text = (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8")
        self.assertIn(digest, text.lower())
        self.assertIn("NeonMiner.exe", text)

    def test_readme_hash_matches(self):
        digest = hashlib.sha256(self.data).hexdigest()
        readme = (DIST / "README-Windows.md").read_text(encoding="utf-8")
        self.assertIn(digest, readme, "в инструкции устаревший SHA256")

    def test_embedded_web_ui(self):
        # В exe зашиты HTML/CSS/JS — внешних файлов быть не должно.
        for needle in (b"NEON", b"static/app.js", b"/api/start", b"consent"):
            self.assertIn(needle, self.data, f"в exe нет {needle!r}")

    def test_wallet_generator_is_embedded(self):
        # /wallet отдаётся локальным сервером как text/html — без GitHub raw и CDN.
        for needle in (b"/wallet", b'content="default-src', b"getRandomValues",
                       b"window.NM_WORDS", b"44bUDLb1Fb8W"):
            self.assertIn(needle, self.data, f"в exe нет встроенного генератора: {needle!r}")

    def test_embedded_pool_catalog(self):
        for name in (b"c3pool", b"hashvault", b"supportxmr", b"moneroocean"):
            self.assertIn(name, self.data)

    def test_windows_api_imports(self):
        for dll in (b"ws2_32", b"WINHTTP", b"SHELL32"):
            self.assertTrue(
                any(dll.lower() in chunk.lower() for chunk in self.data.split(b"\0")),
                f"в импортах нет {dll!r}")

    def test_no_hardcoded_wallet(self):
        # Тестовый адрес не должен попасть в exe как «кошелёк по умолчанию».
        self.assertNotIn(b"43QGgipcHvNLBX3nunZLwVQpF6VbobmGcQKzXzQ5xMf", self.data)

    def test_no_autostart_registry_strings(self):
        for bad in (b"CurrentVersion\\Run", b"schtasks.exe", b"HKCU\\Software"):
            self.assertNotIn(bad, self.data)

    def test_version_in_readme(self):
        readme = (DIST / "README-Windows.md").read_text(encoding="utf-8")
        self.assertIn("SmartScreen", readme)
        self.assertIn("SHA256", readme)
        self.assertIn("%LOCALAPPDATA%", readme)
        self.assertIn("C3Pool", readme)


class SourceSanityTest(unittest.TestCase):
    def test_source_exists_and_documents_consent(self):
        src = (ROOT / "windows" / "neonminer.c").read_text(encoding="utf-8")
        self.assertIn("consent", src)
        self.assertIn("SHA256", src)
        self.assertIn("WinHttpOpen", src)
        self.assertIn("TerminateProcess", src)
        # Никаких скрытых фоновых режимов в конфиге.
        self.assertIn('\\"background\\": false', src)

    def test_build_script_reproducible_demo_timestamp(self):
        script = (ROOT / "windows" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn("view[\"updated_at\"] = 1793040612", script)
        self.assertRegex(script, r"x86_64-windows-gnu")

    def test_web_ui_has_consent_gate(self):
        html = (ROOT / "windows" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="consent"', html)
        self.assertIn('id="startBtn"', html)
        js = (ROOT / "windows" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("consent", js)
        self.assertIn("/api/start", js)
        self.assertIn("simNote", js)

    def test_no_cdn_references(self):
        html = (ROOT / "windows" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", html.replace("http://127.0.0.1", ""))
        self.assertNotIn("cdn.", html)
        self.assertNotIn("<script src=\"http", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
