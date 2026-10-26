"""Синтетические «скриншоты» игровых полей разных стилей — для тестов распознавания."""
from __future__ import annotations

import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from seabattle.rules import HIT, MISS, SUNK, UNKNOWN

STYLES = ("notebook", "dark", "water", "flat")


def _font(size):
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def draw_board(img: Image.Image, x0: int, y0: int, cell: int, states, style: str,
               my_ships=None, dim: float = 0.0, hover=None, rng=None):
    """Рисует поле 10×10 с отметками. (x0, y0) — левый верхний угол клеток."""
    rng = rng or random.Random(0)
    d = ImageDraw.Draw(img, "RGBA")
    n = len(states)
    W = cell * n
    f = _font(max(9, cell // 2))
    # подписи (буквы/цифры) снаружи сетки
    lab = (60, 60, 60) if style in ("notebook", "flat") else (200, 210, 230)
    for k in range(n):
        d.text((x0 + k * cell + cell * 0.3, y0 - cell * 0.85), "ABCDEFGHIJ"[k], fill=lab, font=f)
        d.text((x0 - cell * 0.95, y0 + k * cell + cell * 0.2), str(k + 1), fill=lab, font=f)

    if style == "notebook":
        d.rectangle([x0, y0, x0 + W, y0 + W], fill=(255, 255, 255))
    elif style == "dark":
        d.rectangle([x0 - 2, y0 - 2, x0 + W + 2, y0 + W + 2], fill=(18, 26, 52))
    elif style == "water":
        arr = np.asarray(img).copy()
        yy, xx = np.mgrid[0:W, 0:W]
        tex = 18 * np.sin(xx / 5.0) * np.cos(yy / 7.0) + np.random.default_rng(3).normal(0, 6, (W, W))
        base = np.stack([40 + tex, 110 + tex, 170 + tex], axis=-1)
        arr[y0:y0 + W, x0:x0 + W] = np.clip(base, 0, 255).astype(np.uint8)
        img.paste(Image.fromarray(arr))
        d = ImageDraw.Draw(img, "RGBA")
    elif style == "flat":
        d.rectangle([x0, y0, x0 + W, y0 + W], fill=(250, 250, 250))

    for r in range(n):
        for c in range(n):
            cx0, cy0 = x0 + c * cell, y0 + r * cell
            cx1, cy1 = cx0 + cell, cy0 + cell
            mx, my = (cx0 + cx1) / 2, (cy0 + cy1) / 2
            s = states[r][c]
            ship = my_ships is not None and (r, c) in my_ships
            if style == "dark":
                col = (44, 64, 118)
                if s == MISS:
                    col = (34, 50, 92)
                if s == SUNK:
                    col = (110, 20, 30)
                d.rounded_rectangle([cx0 + 2, cy0 + 2, cx1 - 2, cy1 - 2], radius=max(2, cell // 8), fill=col)
            if style == "flat":
                col = (222, 226, 232)
                if s == MISS:
                    col = (160, 170, 182)
                elif s == HIT:
                    col = (232, 60, 60)
                elif s == SUNK:
                    col = (120, 20, 20)
                d.rectangle([cx0 + 1, cy0 + 1, cx1 - 1, cy1 - 1], fill=col)
            if ship and s == UNKNOWN:
                d.rectangle([cx0 + 3, cy0 + 3, cx1 - 3, cy1 - 3], fill=(120, 120, 200))
            rad = max(2, int(cell * 0.11))
            if s == MISS:
                if style == "notebook":
                    d.ellipse([mx - rad, my - rad, mx + rad, my + rad], fill=(70, 70, 70))
                elif style == "dark":
                    d.ellipse([mx - rad, my - rad, mx + rad, my + rad], fill=(220, 230, 255))
                elif style == "water":
                    d.ellipse([mx - rad * 2, my - rad * 2, mx + rad * 2, my + rad * 2],
                              outline=(240, 250, 255), width=max(1, cell // 14))
            elif s in (HIT, SUNK):
                if style == "notebook":
                    if s == SUNK:
                        d.rectangle([cx0 + 1, cy0 + 1, cx1 - 1, cy1 - 1], fill=(255, 215, 215))
                    k = cell * 0.22
                    wdt = max(2, cell // 12)
                    d.line([cx0 + k, cy0 + k, cx1 - k, cy1 - k], fill=(230, 30, 30), width=wdt)
                    d.line([cx0 + k, cy1 - k, cx1 - k, cy0 + k], fill=(230, 30, 30), width=wdt)
                elif style == "dark":
                    rr = cell * 0.3
                    d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=(255, 120, 20))
                    d.ellipse([mx - rr / 2, my - rr / 2, mx + rr / 2, my + rr / 2], fill=(255, 220, 60))
                elif style == "water":
                    if s == SUNK:
                        d.rectangle([cx0 + 2, cy0 + 2, cx1 - 2, cy1 - 2], fill=(90, 90, 96))
                    rr = cell * 0.28
                    d.polygon([(mx, my - rr), (mx + rr * 0.4, my - rr * 0.3), (mx + rr, my),
                               (mx + rr * 0.4, my + rr * 0.3), (mx, my + rr),
                               (mx - rr * 0.4, my + rr * 0.3), (mx - rr, my),
                               (mx - rr * 0.4, my - rr * 0.3)], fill=(250, 90, 30))

    # линии сетки
    if style == "notebook":
        for k in range(n + 1):
            d.line([x0 + k * cell, y0, x0 + k * cell, y0 + W], fill=(180, 180, 255), width=1)
            d.line([x0, y0 + k * cell, x0 + W, y0 + k * cell], fill=(180, 180, 255), width=1)
        d.rectangle([x0, y0, x0 + W, y0 + W], outline=(110, 110, 230), width=1)
    elif style == "water":
        for k in range(n + 1):
            d.line([x0 + k * cell, y0, x0 + k * cell, y0 + W], fill=(20, 50, 90), width=1)
            d.line([x0, y0 + k * cell, x0 + W, y0 + k * cell], fill=(20, 50, 90), width=1)
    elif style == "flat":
        d.rectangle([x0, y0, x0 + W, y0 + W], outline=(150, 150, 160), width=1)

    if hover is not None:
        hr, hc = hover
        d.rectangle([x0 + hc * cell + 1, y0 + hr * cell + 1, x0 + (hc + 1) * cell - 1,
                     y0 + (hr + 1) * cell - 1], fill=(255, 255, 0, 40))
    if dim > 0:
        d.rectangle([x0 - 1, y0 - 1, x0 + W + 1, y0 + W + 1], fill=(128, 128, 128, int(255 * dim)))


def make_screen(states, style="notebook", cell=32, size=(1280, 720), offset=(640, 160),
                my_states=None, my_ships=None, dim=0.0, hover=None, clutter=True, seed=0):
    """Скриншот «страницы»: поле соперника по offset, своё поле левее, плюс мусор вокруг."""
    rng = random.Random(seed)
    bgcol = {"notebook": (255, 255, 255), "dark": (12, 16, 30), "water": (8, 40, 70),
             "flat": (236, 238, 242)}[style]
    img = Image.new("RGB", size, bgcol)
    d = ImageDraw.Draw(img)
    if clutter:
        f = _font(16)
        tc = (40, 40, 40) if style in ("notebook", "flat") else (220, 220, 230)
        d.text((40, 30), "Sea battle online - players: 72", fill=tc, font=f)
        d.rectangle([size[0] - 260, 24, size[0] - 40, 60], fill=(90, 160, 90))
        d.text((size[0] - 230, 32), "Play again", fill=(255, 255, 255), font=f)
        d.line([20, size[1] - 50, size[0] - 20, size[1] - 50], fill=(200, 200, 200), width=1)
        for i in range(6):
            d.text((40 + i * 150, size[1] - 40), f"link {i}", fill=(30, 60, 200), font=f)
    n = len(states)
    ox, oy = offset
    my_x = ox - cell * n - cell * 4
    if my_x > cell * 2:
        draw_board(img, my_x, oy, cell, my_states if my_states is not None else
                   [[UNKNOWN] * n for _ in range(n)], style, my_ships=my_ships, rng=rng)
    draw_board(img, ox, oy, cell, states, style, dim=dim, hover=hover, rng=rng)
    return np.asarray(img).copy()
