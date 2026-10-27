"""Синтетические «скриншоты» игровых полей разных стилей — для тестов распознавания."""
from __future__ import annotations

import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from seabattle.rules import HIT, MISS, SUNK, UNKNOWN

STYLES = ("notebook", "dark", "water", "flat", "paper")

# «paper» — как на Яндекс Играх: тетрадный лист, клетки страницы совпадают с клетками полей,
# поле обведено жирной синей рамкой, сверху цифры, слева буквы, промах — чернильная клякса,
# попадание — красный крест, убитый корабль прорисован под крестами.
PAPER_LINE = (168, 181, 181)
NAVY = (36, 44, 150)


def _font(size):
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _blot(d, mx, my, rad, rng):
    """Чернильная клякса: плотная середина, рыхлые края."""
    for i in range(7):
        rr = rad * (1.0 - i / 8.0)
        d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=(44, 48, 168, 50 + 25 * i))
    for _ in range(14):
        a = rng.uniform(0, 6.283)
        rr = rad * rng.uniform(0.8, 1.15)
        px, py = mx + rr * np.cos(a), my + rr * np.sin(a)
        d.ellipse([px - 1.5, py - 1.5, px + 1.5, py + 1.5], fill=(60, 64, 180, 120))


def _paper_cross(d, cx0, cy0, cell, rng):
    k = cell * 0.2
    w = max(3, cell // 7)
    j = [rng.uniform(-cell * 0.03, cell * 0.03) for _ in range(4)]
    d.line([cx0 + k + j[0], cy0 + k, cx0 + cell - k, cy0 + cell - k + j[1]], fill=(196, 62, 40), width=w)
    d.line([cx0 + k, cy0 + cell - k + j[2], cx0 + cell - k + j[3], cy0 + k], fill=(196, 62, 40), width=w)


def _paper_ship(d, cx0, cy0, cell, sides):
    """Кусок нарисованного корабля в клетке; sides — с каких сторон корпус продолжается."""
    ins = {s_: (0 if sides.get(s_) else cell * 0.14) for s_ in ("l", "r", "t", "b")}
    box = [cx0 + ins["l"], cy0 + ins["t"], cx0 + cell - ins["r"], cy0 + cell - ins["b"]]
    d.rectangle(box, fill=(232, 234, 252), outline=NAVY, width=max(2, cell // 16))
    rr = cell * 0.13
    mx, my = cx0 + cell / 2, cy0 + cell / 2
    d.ellipse([mx - rr, my - rr, mx + rr, my + rr], outline=NAVY, width=max(1, cell // 20))


def paper_background(img: Image.Image, cell: int, origin):
    """Клетки тетрадного листа через всю страницу (совпадают с клетками полей)."""
    d = ImageDraw.Draw(img)
    W, H = img.size
    x = origin[0] % cell
    while x < W:
        d.line([x, 0, x, H], fill=PAPER_LINE)
        x += cell
    y = origin[1] % cell
    while y < H:
        d.line([0, y, W, y], fill=PAPER_LINE)
        y += cell


def draw_board(img: Image.Image, x0: int, y0: int, cell: int, states, style: str,
               my_ships=None, dim: float = 0.0, hover=None, rng=None, right_labels=False):
    """Рисует поле 10×10 с отметками. (x0, y0) — левый верхний угол клеток."""
    rng = rng or random.Random(0)
    d = ImageDraw.Draw(img, "RGBA")
    n = len(states)
    W = cell * n
    f = _font(max(9, cell // 2))
    # подписи (буквы/цифры) снаружи сетки
    lab = (60, 60, 60) if style in ("notebook", "flat") else (200, 210, 230)
    if style == "paper":
        fp = _font(max(10, int(cell * 0.6)))
        for k in range(n):
            t = str(k + 1)
            tw = d.textlength(t, font=fp)
            d.text((x0 + k * cell + (cell - tw) / 2, y0 - cell * 0.9), t, fill=NAVY, font=fp)
            for xl in ([x0 + W] if right_labels else [x0 - cell]):     # у поля соперника буквы справа
                tw = d.textlength("ABCDEFGHIJ"[k], font=fp)
                d.text((xl + (cell - tw) / 2, y0 + k * cell + cell * 0.12), "ABCDEFGHIJ"[k],
                       fill=NAVY, font=fp)
    else:
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
            if style == "paper":
                def same(rr_, cc_, what):
                    return 0 <= rr_ < n and 0 <= cc_ < n and what(rr_, cc_)
                if s == SUNK or (ship and s == UNKNOWN):
                    if s == SUNK:
                        what = (lambda a_, b_: states[a_][b_] == SUNK)
                    else:
                        what = (lambda a_, b_: (a_, b_) in my_ships and states[a_][b_] == UNKNOWN)
                    _paper_ship(d, cx0, cy0, cell, {"l": same(r, c - 1, what), "r": same(r, c + 1, what),
                                                    "t": same(r - 1, c, what), "b": same(r + 1, c, what)})
                if s == MISS:
                    _blot(d, mx, my, cell * 0.38, rng)
                elif s in (HIT, SUNK):
                    _paper_cross(d, cx0, cy0, cell, rng)
                continue
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
    elif style == "paper":
        t = max(2, cell // 14)
        d.rectangle([x0 - t // 2, y0 - t // 2, x0 + W + (t - 1) // 2, y0 + W + (t - 1) // 2],
                    outline=NAVY, width=t)

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
             "flat": (236, 238, 242), "paper": (255, 255, 255)}[style]
    img = Image.new("RGB", size, bgcol)
    if style == "paper":
        paper_background(img, cell, offset)
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
    gap = 1 if style == "paper" else 4
    my_x = ox - cell * n - cell * gap
    if my_x > cell * 2:
        draw_board(img, my_x, oy, cell, my_states if my_states is not None else
                   [[UNKNOWN] * n for _ in range(n)], style, my_ships=my_ships, rng=rng)
        if style == "paper":                      # стрелка «чей ход» между полями
            ax, ay = ox - cell * 0.75, oy + cell * 5.2
            d.line([ax, ay, ax + cell * 0.45, ay + cell * 0.35, ax, ay + cell * 0.7],
                   fill=(20, 160, 20), width=max(3, cell // 7))
    draw_board(img, ox, oy, cell, states, style, dim=dim, hover=hover, rng=rng,
               right_labels=(style == "paper"))
    return np.asarray(img).copy()
