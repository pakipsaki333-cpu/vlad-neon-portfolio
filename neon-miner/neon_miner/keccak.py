"""Минимальная реализация Keccak-256 (не SHA3-256!) на чистом Python.

Monero использует оригинальный Keccak (домен-паттинг 0x01), а `hashlib.sha3_256`
— это NIST SHA3 (домен 0x06). Результаты разные, поэтому без этого модуля
нельзя проверить контрольную сумму XMR-адреса.

Стандарт: FIPS 202 / Keccak team, rate = 1088 бит (136 байт), capacity = 512 бит.
"""

from __future__ import annotations

_MASK64 = (1 << 64) - 1

# Раундовые константы Keccak-f[1600].
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

# Сдвиги Rho, индексация r[x][y].
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rol(value: int, shift: int) -> int:
    shift %= 64
    if shift == 0:
        return value & _MASK64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _f1600(state: list[list[int]]) -> list[list[int]]:
    """Один полный Keccak-f[1600] (24 раунда)."""
    a = state
    for round_index in range(24):
        # Theta
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]

        # Rho + Pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])

        # Chi
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y] & _MASK64) & b[(x + 2) % 5][y])

        # Iota
        a[0][0] ^= _RC[round_index]

    return a


def keccak_256(data: bytes) -> bytes:
    """Возвращает 32 байта Keccak-256."""
    rate = 136
    state = [[0] * 5 for _ in range(5)]

    # Паддинг Keccak: 0x01 ... 0x00 ... 0x80 (domain = 0x01).
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        state = _f1600(state)

    out = bytearray()
    for i in range(4):  # 4 * 8 = 32 байта
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)
