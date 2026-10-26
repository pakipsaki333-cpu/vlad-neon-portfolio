"""Проверка Monero (XMR) адресов.

Это не «просто regex на 95 символов»: адрес декодируется из Monero Base58
(блочный вариант, не Bitcoin Base58Check) и проверяется 4-байтовая
контрольная сумма Keccak-256. Опечатка в одном символе == почти гарантированная
потеря монет, поэтому проверяем по-настоящему.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional

from .keccak import keccak_256

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}

# В Monero Base58 блок из N байт кодируется в encoded_block_sizes[N] символов.
ENCODED_BLOCK_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]
FULL_BLOCK_SIZE = 8
FULL_ENCODED_BLOCK_SIZE = 11

# Префиксы из cryptonote_config.h.
MAINNET_PRIMARY = 18        # 0x12 — обычный адрес, 95 символов, начинается с «4»
MAINNET_INTEGRATED = 19     # 0x13 — с payment id, 106 символов
MAINNET_SUBADDRESS = 42     # 0x2a — субадрес, 95 символов, начинается с «8»
TESTNET_TAGS = {24, 25, 35, 53, 54, 63}  # testnet/stagenet — в пулы нельзя

_XMR_RE = re.compile(r"^[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+$")


class AddressError(ValueError):
    """Некорректный адрес."""


@dataclass
class AddressInfo:
    valid: bool
    address: str = ""
    network: str = ""          # mainnet | testnet | unknown
    kind: str = ""             # primary | subaddress | integrated | unknown
    length: int = 0
    checksum_ok: bool = False
    payment_id_suffix: Optional[str] = None
    reason: str = ""
    warning: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def decode_monero_base58(value: str) -> bytes:
    """Декодирует Monero Base58 в сырые байты (включая 4 байта checksum)."""
    if not value or not _XMR_RE.match(value):
        raise AddressError("Недопустимые символы в адресе (посторонние знаки, пробелы?).")

    result = bytearray()
    pos = 0
    size = len(value)
    while pos < size:
        remaining = size - pos
        if remaining >= FULL_ENCODED_BLOCK_SIZE:
            chunk = value[pos:pos + FULL_ENCODED_BLOCK_SIZE]
            out_size = FULL_BLOCK_SIZE
        else:
            chunk = value[pos:]
            try:
                out_size = ENCODED_BLOCK_SIZES.index(len(chunk))
            except ValueError as exc:
                raise AddressError("Некорректная длина последнего блока Base58.") from exc
            if out_size == 0:
                raise AddressError("Пустой блок Base58.")

        num = 0
        for char in chunk:
            num = num * 58 + _INDEX[char]
        if num >= (1 << (8 * out_size)):
            raise AddressError("Переполнение блока Base58 — адрес повреждён.")
        result += num.to_bytes(out_size, "big")
        pos += len(chunk)

    return bytes(result)


def mask_address(value: str, head: int = 8, tail: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return value
    return f"{value[:head]}…{value[-tail:]}"


def validate_address(raw: str) -> AddressInfo:
    """Полная проверка адреса. Никогда не бросает исключение наружу."""
    value = (raw or "").strip()
    info = AddressInfo(valid=False, address=value, length=len(value))

    if not value:
        info.reason = "Вставь свой XMR-адрес."
        return info

    # Некоторые пулы принимают «адрес.платёжныйID». База проверяется отдельно.
    payment_id = None
    if "." in value:
        base, suffix = value.split(".", 1)
        if suffix:
            payment_id = suffix
            value = base
            info.length = len(value)
    info.payment_id_suffix = payment_id

    if len(value) not in (95, 106):
        info.reason = f"Длина адреса {len(value)} симв., нужно 95 (обычный/субадрес) или 106 (интегрированный)."
        return info

    try:
        decoded = decode_monero_base58(value)
    except AddressError as exc:
        info.reason = str(exc)
        return info

    if len(decoded) not in (69, 77):
        info.reason = "Неожиданная длина декодированных данных."
        return info

    payload, checksum = decoded[:-4], decoded[-4:]
    if keccak_256(payload)[:4] != checksum:
        info.reason = "Контрольная сумма не совпадает — в адресе опечатка! Деньги уйдут в никуда."
        return info

    info.checksum_ok = True
    tag = payload[0]
    if tag == MAINNET_PRIMARY:
        info.network, info.kind = "mainnet", "primary"
    elif tag == MAINNET_SUBADDRESS:
        info.network, info.kind = "mainnet", "subaddress"
    elif tag == MAINNET_INTEGRATED:
        info.network, info.kind = "mainnet", "integrated"
    elif tag in TESTNET_TAGS:
        info.network, info.kind = "testnet", "unknown"
        info.reason = "Это тестовый/stagenet-адрес. Пулы принимают только mainnet."
        return info
    else:
        info.network, info.kind = "unknown", "unknown"
        info.reason = f"Неизвестный префикс сети ({tag})."
        return info

    # Длина должна соответствовать типу: integrated == 77 байт / 106 симв.
    if info.kind == "integrated" and len(decoded) != 77:
        info.reason = "Интегрированный адрес должен быть длиной 106 символов."
        return info
    if info.kind != "integrated" and len(decoded) != 69:
        info.reason = "Обычный адрес/субадрес должен быть длиной 95 символов."
        return info

    info.valid = True
    info.address = value
    if info.kind == "integrated":
        info.warning = (
            "Интегрированный адрес (106 симв.) — это часто адрес биржи. Многие пулы "
            "его не принимают. Возьми обычный адрес из своего кошелька."
        )
    elif payment_id:
        info.warning = (
            "Суффикс .paymentID устарел и большинством пулов игнорируется. "
            "Используй просто адрес."
        )
    else:
        info.warning = "Не используй депозитный адрес биржи — только свой кошелёк, где есть приватные ключи."
    return info


def require_valid(raw: str) -> str:
    """Строгая версия для CLI: возвращает адрес или бросает AddressError."""
    info = validate_address(raw)
    if not info.valid:
        raise AddressError(info.reason)
    return info.address
