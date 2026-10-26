"""Тесты офлайн-генератора XMR-кошелька.

Проверяем не «на глаз», аagainst официального вектора Monero и
независимых реализаций (Node WebCrypto + Python monero-пакет в CI-подобном прогоне).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from neon_miner.address import validate_address  # noqa: E402

DIST = ROOT / "dist"
HTML = DIST / "NeonMiner-wallet.html"
CRYPTO = ROOT / "windows" / "web" / "wallet_crypto.js"
WORDLIST = ROOT / "windows" / "wordlist_en.json"

# Официальный тест-вектор (monero-project/monero + пакет `monero`):
VECTOR_SEED = " ".join(["plus"] * 25)
VECTOR_SPEND = "3b040000" * 8
VECTOR_VIEW = "43a8a7715eed11eff145a2024ddcc39740255156da7bbd736ee66a0838053a02"
VECTOR_SPEND_PUB = "4e6b660ef63933b07a01786992185bcb721720feff611aef4eb0ac38a50b5eab"
VECTOR_VIEW_PUB = "58cdf6b28afa805423cf410d78bf908a416eaea1a2eef05def1aae4b7596f3de"
VECTOR_ADDRESS = ("44bUDLb1Fb8WX3YoikyhCnb2gK86q9RGuh2ainXfMtUMVfHDELAP8FyF5GDCF"
                  "DuKRyQ8FeGj2QkMuGiH6RgrhN4aS87C6VR")

NODE_SCRIPT = r"""
const fs = require('fs');
const crypto = require('crypto');
const words = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
globalThis.window = globalThis;
eval(fs.readFileSync(process.argv[3], 'utf8'));
const C = globalThis.NMWalletCrypto;
const hex = b => Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
const v = C.walletFromMnemonic(('plus '.repeat(24) + 'plus'), words);
const random = [];
for (let i = 0; i < 3; i++) {
  const ent = crypto.randomBytes(64);
  const w = C.walletFromSpend(C.keccak256(new Uint8Array(ent)), words);
  const back = C.walletFromMnemonic(w.mnemonic, words);
  random.push({ address: w.address, seed: w.mnemonic, spend: w.spendKey,
                valid: C.verifyAddressChecksum(w.address),
                restore: back.address === w.address && back.mnemonic === w.mnemonic,
                words: w.mnemonic.split(' ').length });
}
// ed25519 scalarMultBase против WebCrypto
let ed = [];
for (let i = 0; i < 2; i++) {
  const kp = crypto.generateKeyPairSync('ed25519');
  const seed = Buffer.from(kp.privateKey.export({format:'jwk'}).d, 'base64url');
  const h = crypto.createHash('sha512').update(seed).digest();
  h[0] &= 248; h[31] &= 63; h[31] |= 64;
  const ours = Buffer.from(C.encodePoint(C.scalarMultBase(new Uint8Array(h.subarray(0,32))))).toString('base64url');
  ed.push(ours === kp.publicKey.export({format:'jwk'}).x);
}
console.log(JSON.stringify({
  keccakEmpty: hex(C.keccak256(new TextEncoder().encode(''))),
  keccakAbc: hex(C.keccak256(new TextEncoder().encode('abc'))),
  words: words.length, words1083: words[1083],
  v: { address: v.address, spend: v.spendKey, view: v.viewKey,
       spendPub: v.spendPub, viewPub: v.viewPub, seed: v.mnemonic },
  badChecksum: C.verifyAddressChecksum(v.address.slice(0,40) + (v.address[40]==='A'?'B':'A') + v.address.slice(41)),
  vectorAddrValid: C.verifyAddressChecksum(v.address),
  random, ed,
}));
"""


def run_node() -> dict:
    if not shutil.which("node"):
        raise unittest.SkipTest("Node.js не найден — пропускаем JS-векторные тесты")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "t.js"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        out = subprocess.run(["node", str(script), str(WORDLIST), str(CRYPTO)],
                             capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError(f"node failed: {out.stderr[-2000:]}")
        return json.loads(out.stdout.strip().splitlines()[-1])


class WalletSourceTest(unittest.TestCase):
    def test_zip_is_selfcontained(self):
        """Архив — рабочий способ скачать страницу одним кликом."""
        import zipfile
        zpath = DIST / "NeonMiner-wallet.zip"
        if not zpath.exists():
            self.skipTest("архив не собран")
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            self.assertIn("NeonMiner-wallet.html", names)
            self.assertTrue(any(n.endswith(".txt") for n in names))
            inner = z.read("NeonMiner-wallet.html").decode("utf-8")
        self.assertEqual(inner, HTML.read_text(encoding="utf-8"),
                         "HTML внутри архива отличается от основного файла")
        self.assertIn("Content-Security-Policy", inner)
        digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
        self.assertIn(digest, (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8"))

    def test_wordlist_is_official(self):
        words = json.loads(WORDLIST.read_text(encoding="utf-8"))
        self.assertEqual(len(words), 1626)
        self.assertEqual(words[0], "abbey")
        self.assertEqual(words[-1], "zoom")
        self.assertEqual(words[1083], "plus")
        self.assertEqual(len(set(words)), 1626)
        self.assertTrue(all(w.isalpha() and w.islower() for w in words))

    def test_html_is_offline_and_selfcontained(self):
        if not HTML.exists():
            self.skipTest("dist/NeonMiner-wallet.html не собран")
        html = HTML.read_text(encoding="utf-8")
        for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon",
                    "importScripts", "<script src", "<link href", "http://", "https://"):
            self.assertNotIn(bad, html, f"сетевой вызов/ссылка: {bad}")
        self.assertIn("connect-src 'none'", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("getRandomValues", html)
        self.assertIn("window.NM_WORDS", html)

    def test_html_contains_tested_crypto(self):
        if not HTML.exists():
            self.skipTest("dist/NeonMiner-wallet.html не собран")
        html = HTML.read_text(encoding="utf-8")
        self.assertIn(CRYPTO.read_text(encoding="utf-8"), html,
                      "в собранном HTML лежит не тот crypto-код, что тестируется")
        words = json.loads(WORDLIST.read_text(encoding="utf-8"))
        self.assertIn(json.dumps(words, ensure_ascii=False, separators=(",", ":")), html)

    def test_sha256sums_contains_wallet(self):
        if not HTML.exists():
            self.skipTest("dist/NeonMiner-wallet.html не собран")
        digest = hashlib.sha256(HTML.read_bytes()).hexdigest()
        sums = (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8")
        self.assertIn(digest, sums)
        self.assertIn("NeonMiner-wallet.html", sums)


class WalletCryptoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run_node()

    def test_keccak_vectors(self):
        self.assertEqual(self.r["keccakEmpty"],
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(self.r["keccakAbc"],
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_ed25519_matches_webcrypto(self):
        self.assertTrue(all(self.r["ed"]), "scalarMultBase расходится с Node WebCrypto Ed25519")

    def test_official_mnemonic_vector(self):
        v = self.r["v"]
        self.assertEqual(v["seed"], VECTOR_SEED)
        self.assertEqual(v["spend"], VECTOR_SPEND)
        self.assertEqual(v["view"], VECTOR_VIEW)
        self.assertEqual(v["spendPub"], VECTOR_SPEND_PUB)
        self.assertEqual(v["viewPub"], VECTOR_VIEW_PUB)
        self.assertEqual(v["address"], VECTOR_ADDRESS)
        self.assertTrue(self.r["vectorAddrValid"])
        self.assertFalse(self.r["badChecksum"])

    def test_vector_address_passes_python_validator(self):
        info = validate_address(VECTOR_ADDRESS)
        self.assertTrue(info.valid, info.reason)
        self.assertTrue(info.checksum_ok)
        self.assertEqual(info.network, "mainnet")

    def test_random_wallets_are_valid_and_restorable(self):
        self.assertEqual(len(self.r["random"]), 3)
        for w in self.r["random"]:
            self.assertTrue(w["valid"], w["address"])
            self.assertTrue(w["restore"], "сид не восстанавливает адрес")
            self.assertEqual(w["words"], 25)
            info = validate_address(w["address"])
            self.assertTrue(info.valid, info.reason)
            self.assertEqual(info.kind, "primary")
            self.assertTrue(w["address"].startswith("4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
