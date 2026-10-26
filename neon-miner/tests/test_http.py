"""Интеграционные тесты HTTP-сервера (живой localhost, без внешней сети)."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neon_miner.server import make_server  # noqa: E402

VALID = "43QGgipcHvNLBX3nunZLwVQpF6VbobmGcQKzXzQ5xMfJgzfRBzfXcJHX1tUHcKPm9bcjubrzKqTm69JbQSL4B3f6E3mNCbU"


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def call(self, path, payload=None):
        if payload is None:
            req = urllib.request.Request(self.base + path)
        else:
            req = urllib.request.Request(
                self.base + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_index_html(self):
        status, ctype, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"NEON", body)

    def test_static_assets(self):
        for path, needle in (("/static/style.css", b":root"),
                             ("/static/app.js", b"NeonMiner"),
                             ("/static/favicon.svg", b"<svg")):
            status, _, body = self.call(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body, path)

    def test_path_traversal_blocked(self):
        status, _, _ = self.call("/static/../configgen.py")
        self.assertEqual(status, 404)

    def test_unknown_api_is_json_404(self):
        status, ctype, body = self.call("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", ctype)
        self.assertIn(b"error", body)

    def test_full_client_flow(self):
        status, _, body = self.call("/api/catalog")
        self.assertEqual(status, 200)
        pools = json.loads(body)["pools"]
        self.assertEqual(len(pools), 4)

        status, _, body = self.call("/api/validate?address=" + VALID)
        self.assertTrue(json.loads(body)["valid"])

        status, _, body = self.call("/api/stats?pool=c3pool&demo=1")
        self.assertTrue(json.loads(body)["demo"])

        status, _, body = self.call("/api/config", {
            "wallet": VALID, "pool": "c3pool", "port": 33333, "worker": "test-rig"})
        cfg = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn(VALID, cfg["config_json"])
        self.assertTrue(cfg["tls"])

        status, _, body = self.call("/api/calc", {"hashrate_hs": 5000})
        self.assertEqual(status, 200)

    def test_no_ssrf_arbitrary_url(self):
        from neon_miner.http_client import fetch_json
        data, err = fetch_json("https://evil.example.com/steal")
        self.assertIsNone(data)
        self.assertIn("белый список", err)

    def test_big_body_rejected(self):
        huge = {"wallet": "A" * 300000}
        status, _, body = self.call("/api/config", huge)
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
