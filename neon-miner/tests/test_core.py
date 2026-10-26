"""Тесты ядра NeonMiner. Запуск из neon-miner/:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neon_miner import adapters, calc, configgen, pools, server  # noqa: E402
from neon_miner.address import decode_monero_base58, mask_address, validate_address  # noqa: E402
from neon_miner.http_client import demo_view, load_fixture  # noqa: E402
from neon_miner.keccak import keccak_256  # noqa: E402

# Публичный адрес из документации пулов — используется только как тестовый вектор.
VALID = "43QGgipcHvNLBX3nunZLwVQpF6VbobmGcQKzXzQ5xMfJgzfRBzfXcJHX1tUHcKPm9bcjubrzKqTm69JbQSL4B3f6E3mNCbU"
CORRUPT = VALID[:40] + ("A" if VALID[40] != "A" else "B") + VALID[41:]


class KeccakTest(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(
            keccak_256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(
            keccak_256(b"abc").hex(),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_differs_from_sha3(self):
        import hashlib
        self.assertNotEqual(keccak_256(b"monero"), hashlib.sha3_256(b"monero").digest())

    def test_long_input_multiblock(self):
        self.assertEqual(len(keccak_256(b"x" * 1000)), 32)


class AddressTest(unittest.TestCase):
    def test_valid_address(self):
        info = validate_address(VALID)
        self.assertTrue(info.valid, info.reason)
        self.assertTrue(info.checksum_ok)
        self.assertEqual(info.network, "mainnet")
        self.assertEqual(info.kind, "primary")

    def test_corrupted_checksum_rejected(self):
        info = validate_address(CORRUPT)
        self.assertFalse(info.valid)
        self.assertFalse(info.checksum_ok)
        self.assertIn("Контрольная сумма", info.reason)

    def test_wrong_length(self):
        self.assertFalse(validate_address(VALID[:-1]).valid)
        self.assertFalse(validate_address("").valid)

    def test_bad_characters(self):
        self.assertFalse(validate_address(VALID[:10] + "0" + VALID[11:]).valid)

    def test_payment_id_suffix(self):
        info = validate_address(VALID + "." + "a" * 16)
        self.assertTrue(info.valid)
        self.assertEqual(info.payment_id_suffix, "a" * 16)
        self.assertIn("paymentID", info.warning)

    def test_decode_length(self):
        self.assertEqual(len(decode_monero_base58(VALID)), 69)

    def test_mask(self):
        self.assertTrue(mask_address(VALID).startswith("43QGgipc"))
        self.assertTrue(mask_address(VALID).endswith("mNCbU"))

    def test_never_raises(self):
        for junk in (None, "!!!", "4" * 94, "1", "https://evil.tld/x"):
            self.assertIsInstance(validate_address(junk), object)


class PoolTest(unittest.TestCase):
    def test_all_pools_have_urls(self):
        for pool_id, pool in pools.POOLS.items():
            self.assertTrue(pool.url_stats(VALID).startswith("https://"), pool_id)
            self.assertIn(VALID, pool.url_payments(VALID))
            self.assertTrue(pool.dashboard(VALID).startswith("https://"), pool_id)
            self.assertTrue(pool.ports)

    def test_fees_and_thresholds_are_sane(self):
        for pool in pools.POOLS.values():
            self.assertGreaterEqual(pool.fee_pct, 0)
            self.assertLess(pool.fee_pct, 5)
            self.assertGreater(pool.min_payout_xmr, 0)
            self.assertGreaterEqual(pool.default_payout_xmr, pool.min_payout_xmr)

    def test_recommended_pool_has_lowest_threshold(self):
        best = min(p.min_payout_xmr for p in pools.POOLS.values())
        self.assertEqual(pools.POOLS[pools.DEFAULT_POOL].min_payout_xmr, best)

    def test_catalog_serializable(self):
        json.dumps(pools.catalog())


class AdapterTest(unittest.TestCase):
    def test_nodejs_pool_normalization(self):
        view = adapters.normalize_view(
            "c3pool",
            stats={"hash": 5000, "amtDue": 500_000_000, "amtPaid": 1_000_000_000,
                   "validShares": 12, "invalidShares": 1, "totalHashes": 1e12,
                   "lastHash": 1793040000, "txnCount": 1},
            payments=[{"ts": 1793040000, "amount": 1_000_000_000, "txnHash": "a" * 64}],
            pool_stats={"pool_statistics": {"hashRate": 4.9e8, "miners": 2200},
                        "price": {"usd": 594, "eur": 531}},
        )
        self.assertEqual(view["balance_xmr"], 0.0005)
        self.assertEqual(view["paid_xmr"], 0.001)
        self.assertEqual(view["hashrate_hs"], 5000)
        self.assertEqual(view["price_eur"], 531)
        self.assertEqual(len(view["payments"]), 1)
        self.assertEqual(view["progress_pct"], 50.0)
        self.assertFalse(view["demo"])

    def test_hashvault_normalization(self):
        view = adapters.normalize_view(
            "hashvault",
            stats={"collective": {"hashRate": 4800, "avg24hashRate": 4700,
                                 "lastShare": 1793040000000, "totalHashes": 1e12,
                                 "validShares": 100, "invalidShares": 2},
                   "revenue": {"confirmedBalance": 750_000_000, "totalPaid": 2_000_000_000,
                               "payoutThreshold": 1_000_000_000, "totalPaymentsSent": 2,
                               "unconfirmedBalance": {"collective": {"total": 250_000_000}}}},
            payments=[{"ts": 1793040000, "amount": 1_000_000_000, "txnHash": "b" * 64}],
            pool_stats={"pool_statistics": {"collective": {"hashRate": 6.8e8, "miners": 3800}},
                        "network_statistics": {"difficulty": 826_300_623_048,
                                               "value": 607_969_680_000},
                        "market": {"price_usd": 593.9, "price_eur": 530.85, "price_rub": 47988}},
        )
        self.assertAlmostEqual(view["balance_xmr"], 0.00075)
        self.assertEqual(view["last_share_ts"], 1793040000)
        self.assertEqual(view["threshold_xmr"], 0.001)
        self.assertAlmostEqual(view["unconfirmed_xmr"], 0.00025)
        self.assertAlmostEqual(view["network_hashrate_hs"], 826_300_623_048 / 120)
        self.assertAlmostEqual(view["block_reward_xmr"], 0.60796968)
        self.assertEqual(view["price_rub"], 47988)

    def test_false_instead_of_zero(self):
        view = adapters.normalize_view("c3pool", stats={"hash": False, "amtDue": False})
        self.assertEqual(view["hashrate_hs"], 0)
        self.assertEqual(view["balance_xmr"], 0)

    def test_payments_sorted_desc(self):
        pays = [{"ts": 100, "amount": 1}, {"ts": 300, "amount": 1}, {"ts": 200, "amount": 1}]
        out = adapters.normalize_payments("c3pool", pays)
        self.assertEqual([p["ts"] for p in out], [300, 200, 100])

    def test_all_demo_fixtures_normalize(self):
        for pool_id in pools.POOLS:
            view = demo_view(pool_id)
            self.assertTrue(view["demo"])
            self.assertGreater(view["paid_xmr"], 0)
            self.assertGreater(view["hashrate_hs"], 0)
            self.assertGreater(view["price_eur"], 0)
            self.assertIsInstance(load_fixture(pool_id, "stats"), dict)


class ConfigGenTest(unittest.TestCase):
    def test_config_contains_wallet_and_pool(self):
        b = configgen.bundle(VALID, "c3pool")
        self.assertEqual(b["config"]["pools"][0]["user"], VALID)
        self.assertEqual(b["config"]["pools"][0]["url"], "auto.c3pool.org:13333")
        self.assertFalse(b["tls"])
        self.assertIn(VALID, b["cmd_linux"])
        self.assertTrue(b["config"]["cpu"]["enabled"])

    def test_tls_port_c3pool_uses_dedicated_host(self):
        b = configgen.bundle(VALID, "c3pool", 33333)
        self.assertEqual(b["host"], "mine.c3pool.com:33333")
        self.assertTrue(b["tls"])
        self.assertTrue(b["config"]["pools"][0]["tls"])

    def test_hashvault_has_failover_pool(self):
        b = configgen.bundle(VALID, "hashvault")
        self.assertEqual(b["config"]["pools"][1]["url"], "pool.hashvault.sh:443")
        self.assertTrue(b["config"]["pools"][1]["tls"])

    def test_threads_and_donate_clamped(self):
        b = configgen.bundle(VALID, "c3pool", threads_pct=999, donate_level=-5)
        self.assertEqual(b["config"]["cpu"]["max-threads-hint"], 100)
        self.assertEqual(b["config"]["donate-level"], 0)

    def test_fixed_difficulty(self):
        b = configgen.bundle(VALID, "c3pool", fixed_difficulty=128000)
        self.assertTrue(b["config"]["pools"][0]["user"].endswith("+128000"))

    def test_worker_sanitized(self):
        b = configgen.bundle(VALID, "c3pool", worker="my pc; rm -rf /")
        self.assertNotIn(" ", b["config"]["pools"][0]["pass"])
        self.assertNotIn(";", b["config"]["pools"][0]["pass"])

    def test_no_hidden_background(self):
        for pool_id in pools.POOLS:
            b = configgen.bundle(VALID, pool_id)
            self.assertFalse(b["config"]["background"])
            self.assertFalse(b["config"]["opencl"])
            self.assertFalse(b["config"]["cuda"])
            self.assertEqual(b["config"]["http"]["host"], "127.0.0.1")
            self.assertTrue(b["config"]["http"]["restricted"])

    def test_config_json_is_valid(self):
        for pool_id in pools.POOLS:
            b = configgen.bundle(VALID, pool_id)
            self.assertIsInstance(json.loads(b["config_json"]), dict)


class CalcTest(unittest.TestCase):
    def test_zero_hashrate(self):
        r = calc.estimate(0)
        self.assertEqual(r["xmr_per_day"], 0)
        self.assertIsNone(r["days_to_payout"])

    def test_fee_reduces_income(self):
        a = calc.estimate(5000, fee_pct=0)
        b = calc.estimate(5000, fee_pct=1)
        self.assertGreater(a["xmr_per_day"], b["xmr_per_day"])

    def test_electricity_makes_it_negative_at_de_prices(self):
        r = calc.estimate(5000, power_watts=65, eur_per_kwh=0.36)
        self.assertFalse(r["profitable"])
        self.assertLess(r["net_eur_per_day"], 0)

    def test_free_electricity_is_profitable(self):
        r = calc.estimate(5000, power_watts=0, eur_per_kwh=0)
        self.assertTrue(r["profitable"])

    def test_days_to_payout_scales(self):
        a = calc.estimate(5000, threshold_xmr=0.001)
        b = calc.estimate(5000, threshold_xmr=0.1)
        self.assertAlmostEqual(b["days_to_payout"] / a["days_to_payout"], 100, places=4)

    def test_difficulty_to_hashrate(self):
        self.assertAlmostEqual(calc.network_hashrate_from_difficulty(826_300_623_048),
                               826_300_623_048 / 120)


class RouteTest(unittest.TestCase):
    def test_health_and_catalog(self):
        payload, status = server.route("GET", "/api/health", {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        payload, status = server.route("GET", "/api/catalog", {}, {})
        self.assertEqual(len(payload["pools"]), 4)

    def test_validate_route(self):
        payload, _ = server.route("GET", "/api/validate", {"address": [VALID]}, {})
        self.assertTrue(payload["valid"])

    def test_config_route_rejects_bad_wallet(self):
        from neon_miner.server import ApiError
        with self.assertRaises(ApiError):
            server.route("POST", "/api/config", {}, {"wallet": CORRUPT, "pool": "c3pool"})

    def test_config_demo_route(self):
        payload, status = server.route("POST", "/api/config", {}, {"demo": True, "pool": "c3pool"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo"])
        self.assertIn("PASTE_YOUR_XMR_ADDRESS_HERE", payload["config_json"])

    def test_calc_route(self):
        payload, status = server.route("POST", "/api/calc", {}, {"hashrate_hs": 5000})
        self.assertEqual(status, 200)
        self.assertIn("net_eur_per_day", payload)

    def test_normalize_route(self):
        payload, status = server.route("POST", "/api/normalize", {}, {
            "pool": "c3pool",
            "stats": {"hash": 1000, "amtDue": 1e9, "amtPaid": 0},
            "payments": [], "pool_stats": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["balance_xmr"], 0.001)

    def test_unknown_route_404(self):
        from neon_miner.server import ApiError
        with self.assertRaises(ApiError) as ctx:
            server.route("GET", "/api/nope", {}, {})
        self.assertEqual(ctx.exception.status, 404)

    def test_stats_demo_when_no_wallet(self):
        payload, status = server.route("GET", "/api/stats", {"pool": ["c3pool"], "demo": ["1"]}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
