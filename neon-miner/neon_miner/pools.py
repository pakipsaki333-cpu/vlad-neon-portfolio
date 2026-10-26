"""Каталог пулов Monero.

Все хосты, порты, комиссии и пороги выплат ниже сверены с живыми API пулов
26.10.2026 (endpoint'ы указаны в `sources`). Ни один адрес не выдуман.

Форматы API бывают двух видов:
  * nodejs_pool — SupportXMR / C3Pool / MoneroOcean (amtDue, amtPaid, hash);
  * hashvault    — собственный API v3 (revenue.confirmedBalance, collective.hashRate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote

PICONERO = 1_000_000_000_000  # 1 XMR = 1e12 piconero
CHECKED_AT = "2026-10-26"


@dataclass(frozen=True)
class Port:
    port: int
    tls: bool
    difficulty: int
    label: str


@dataclass(frozen=True)
class Pool:
    id: str
    name: str
    tagline: str
    fee_pct: float
    min_payout_xmr: float
    default_payout_xmr: float
    region: str
    site: str
    dashboard_tpl: str
    host: str
    ports: List[Port]
    api_kind: str
    api_base: str
    stats_path: str
    payments_path: str
    pool_path: str
    config_path: str
    sources: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    failover_host: Optional[str] = None
    failover_port: Optional[int] = None
    failover_tls: bool = False
    recommended: bool = False

    def url_stats(self, wallet: str) -> str:
        return self.api_base + self.stats_path.format(wallet=quote(wallet, safe=""))

    def url_payments(self, wallet: str) -> str:
        return self.api_base + self.payments_path.format(wallet=quote(wallet, safe=""))

    def url_pool(self) -> str:
        return self.api_base + self.pool_path

    def url_config(self) -> str:
        return self.api_base + self.config_path if self.config_path else ""

    def dashboard(self, wallet: str) -> str:
        return self.dashboard_tpl.format(wallet=quote(wallet, safe=""))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "fee_pct": self.fee_pct,
            "min_payout_xmr": self.min_payout_xmr,
            "default_payout_xmr": self.default_payout_xmr,
            "region": self.region,
            "site": self.site,
            "host": self.host,
            "ports": [{"port": p.port, "tls": p.tls, "difficulty": p.difficulty, "label": p.label}
                      for p in self.ports],
            "api_kind": self.api_kind,
            "api_base": self.api_base,
            "stats_path": self.stats_path,
            "payments_path": self.payments_path,
            "pool_path": self.pool_path,
            "failover": ({"host": self.failover_host, "port": self.failover_port,
                          "tls": self.failover_tls} if self.failover_host else None),
            "recommended": self.recommended,
            "notes": self.notes,
            "sources": self.sources,
            "checked_at": CHECKED_AT,
        }


POOLS: Dict[str, Pool] = {
    "c3pool": Pool(
        id="c3pool",
        name="C3Pool",
        tagline="Самый ранний первый платёж: порог 0.001 XMR и 0% комиссии",
        fee_pct=0.0,
        min_payout_xmr=0.001,
        default_payout_xmr=0.005,
        region="EU / US / Asia (auto-DNS)",
        site="https://c3pool.com/",
        dashboard_tpl="https://c3pool.com/#/dashboard?wallet={wallet}",
        host="auto.c3pool.org",
        ports=[
            Port(13333, False, 72000, "основной · 500+ H/s"),
            Port(80, False, 52000, "слабый CPU / обход файрвола"),
            Port(19999, False, 100000, "мощный CPU"),
            Port(33333, True, 100000, "TLS/SSL (mine.c3pool.com)"),
        ],
        api_kind="nodejs_pool",
        api_base="https://api.c3pool.com",
        stats_path="/miner/{wallet}/stats",
        payments_path="/miner/{wallet}/payments",
        pool_path="/pool/stats",
        config_path="/config",
        sources=[
            "https://api.c3pool.com/config",
            "https://api.c3pool.com/pool/ports",
            "https://github.com/C3Pool/xmrig_setup/blob/master/setup_c3pool_miner.sh",
        ],
        notes=[
            "Порог выплаты 0.001 XMR (поле min_wallet_payout в /config), по умолчанию 0.005 XMR.",
            "TLS-порт 33333 подтверждён в /pool/ports; остальные порты — обычный stratum.",
            "Для порта 33333 используй хост mine.c3pool.com (в API он помечен tls:true).",
            "Пул сам переключает алгоритмы, но платит всегда в XMR.",
        ],
        recommended=True,
    ),
    "hashvault": Pool(
        id="hashvault",
        name="HashVault",
        tagline="Европейский пул с узлом во Франкфурте и подробной статистикой",
        fee_pct=0.9,
        min_payout_xmr=0.001,
        default_payout_xmr=0.11,
        region="Frankfurt / EU / US / Asia",
        site="https://hashvault.pro/monero/",
        dashboard_tpl="https://monero.hashvault.pro/en/dashboard/{wallet}",
        host="pool.hashvault.pro",
        ports=[Port(443, True, 72000, "TLS · авто-выбор ближайшего узла")],
        api_kind="hashvault",
        api_base="https://api.hashvault.pro/v3/monero",
        stats_path="/wallet/{wallet}/stats",
        payments_path="/wallet/{wallet}/payments",
        pool_path="/pool/stats",
        config_path="",
        sources=[
            "https://api.hashvault.pro/v3/monero/pool/stats",
            "https://api.hashvault.pro/v3/monero/pool/ports",
            "https://hashvault.pro/monero/",
        ],
        notes=[
            "Комиссия 0.9% PPLNS и min_wallet_payout 0.001 XMR — из /pool/stats.",
            "Один порт 443 с TLS, стартовая сложность 72000,Difficulty подстраивается автоматически.",
            "В API есть цена XMR в USD/EUR/RUB — используем её для калькулятора.",
            "Есть запасной домен pool.hashvault.sh: добавь его вторым пулом на случай блокировки.",
        ],
        failover_host="pool.hashvault.sh",
        failover_port=443,
        failover_tls=True,
    ),
    "supportxmr": Pool(
        id="supportxmr",
        name="SupportXMR",
        tagline="Крупнейший и самый стабильный пул, но высокий порог выплаты",
        fee_pct=0.6,
        min_payout_xmr=0.1,      # ручная выплата от 0.1 XMR
        default_payout_xmr=0.3,  # автоматическая — от 0.3 XMR
        region="EU (FR/DE) / US / Asia",
        site="https://supportxmr.com/",
        dashboard_tpl="https://supportxmr.com/#/dashboard?wallet={wallet}",
        host="pool.supportxmr.com",
        ports=[
            Port(3333, False, 75000, "слабый CPU · до 30–40 H/s"),
            Port(5555, False, 150000, "средний CPU · до 160 H/s"),
            Port(7777, False, 300000, "мощный CPU / ферма"),
            Port(443, True, 75000, "TLS · обход файрвола"),
            Port(9000, True, 150000, "TLS · альтернативный"),
        ],
        api_kind="nodejs_pool",
        api_base="https://www.supportxmr.com/api",
        stats_path="/miner/{wallet}/stats",
        payments_path="/miner/{wallet}/payments",
        pool_path="/pool/stats",
        config_path="/config",
        sources=[
            "https://www.supportxmr.com/api/config",
            "https://www.supportxmr.com/api/pool/ports",
            "https://www.supportxmr.com/api/network/stats",
        ],
        notes=[
            "Комиссия 0.6% PPLNS; автовыплата от 0.3 XMR, вручную можно запросить от 0.1 XMR.",
            "При 5 kH/s до автовыплаты ~2.5 года — для «капельницы» не лучший выбор.",
            "Порты и сложности взяты из /api/pool/ports; 443 и 9000 — SSL.",
            "Регистрация не нужна: статистика доступна по адресу кошелька.",
        ],
    ),
    "moneroocean": Pool(
        id="moneroocean",
        name="MoneroOcean",
        tagline="Авто-переключение на самый выгодный RandomX-коин, выплата в XMR",
        fee_pct=0.0,
        min_payout_xmr=0.003,
        default_payout_xmr=0.3,
        region="DE / US / JP (geo-DNS)",
        site="https://moneroocean.stream/",
        dashboard_tpl="https://moneroocean.stream/#/dashboard?wallet={wallet}",
        host="gulf.moneroocean.stream",
        ports=[
            Port(10128, False, 128000, "современный CPU/GPU"),
            Port(10032, False, 32000, "слабый CPU"),
            Port(20128, True, 128000, "TLS/SSL"),
            Port(443, True, 128000, "TLS · обход файрвола"),
        ],
        api_kind="nodejs_pool",
        api_base="https://api.moneroocean.stream",
        stats_path="/miner/{wallet}/stats",
        payments_path="/miner/{wallet}/payments",
        pool_path="/pool/stats",
        config_path="/config",
        sources=[
            "https://api.moneroocean.stream/config",
            "https://api.moneroocean.stream/pool/stats",
        ],
        notes=[
            "Комиссия 0% (pplns_fee=0), минимальная выплата 0.003 XMR.",
            "Майнит самый прибыльный RandomX-совместимый коин, но платит XMR.",
            "ВНИМАНИЕ: у пула много доменов-клонов. Официальный — только moneroocean.stream / gulf.moneroocean.stream.",
            "Сайт иногда получает ложные «phishing»-предупреждения из-за ботнетов; API работает стабильно.",
        ],
    ),
}

DEFAULT_POOL = "c3pool"
DEFAULT_PORT = {
    "c3pool": 13333,
    "hashvault": 443,
    "supportxmr": 3333,
    "moneroocean": 10128,
}


def get(pool_id: str) -> Pool:
    if pool_id not in POOLS:
        raise KeyError(f"Неизвестный пул: {pool_id}")
    return POOLS[pool_id]


def catalog() -> list:
    return [pool.as_dict() for pool in POOLS.values()]


def find_port(pool_id: str, port: int) -> Port:
    """Возвращает описание порта или первый порт пула по умолчанию."""
    pool = get(pool_id)
    for item in pool.ports:
        if item.port == port:
            return item
    return pool.ports[0]
