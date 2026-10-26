import random

import numpy as np
import pytest

from seabattle.board import validate_layout
from seabattle.placement import (STRATEGIES, describe, generate, predictability, prior_heatmap,
                                 ships_from_cells)
from seabattle.rules import TOUCH_ANY, TOUCH_CORNERS, TOUCH_NONE, Rules


@pytest.mark.parametrize("strategy", list(STRATEGIES))
@pytest.mark.parametrize("touch", [TOUCH_NONE, TOUCH_CORNERS, TOUCH_ANY])
def test_layouts_are_valid(strategy, touch):
    rules = Rules(touch=touch)
    rng = random.Random(1)
    for _ in range(3):
        ships = generate(strategy, rules, rng)
        assert validate_layout(ships, rules) == []


def test_edges_strategy_puts_big_ships_on_border():
    rules = Rules()
    rng = random.Random(2)
    for _ in range(5):
        ships = generate("edges", rules, rng)
        for s in ships:
            if len(s) >= 2:
                assert all(r in (0, 9) for r, _ in s) or all(c in (0, 9) for _, c in s)


def test_antibot_is_less_predictable_than_random():
    rules = Rules()
    rng = random.Random(3)
    anti = np.mean([predictability(generate("antibot", rules, rng), rules) for _ in range(4)])
    rand = np.mean([predictability(generate("random", rules, rng), rules) for _ in range(8)])
    assert anti < rand - 5


def test_prior_heatmap_shape_and_symmetry():
    heat = prior_heatmap(Rules())
    assert heat.shape == (10, 10)
    assert abs(heat.sum() - 20) < 0.5
    assert abs(heat[0, 0] - heat[9, 9]) < 0.04


def test_describe_and_roundtrip():
    rules = Rules()
    ships = generate("random", rules, random.Random(5))
    lines = describe(ships)
    assert len(lines) == 10 and lines[0].startswith("четырёхпалубный")
    mask = np.zeros((10, 10), bool)
    for s in ships:
        for r, c in s:
            mask[r, c] = True
    good, bad = ships_from_cells(mask)
    assert not bad and sorted(map(len, good), reverse=True) == list(rules.fleet)
