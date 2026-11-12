"""Canonical pickup geometry, consent, fallback and terminal cleanup."""

import pytest

from custom_components.wait_for_wolt.facts import pickup_coordinates


@pytest.mark.parametrize(
    "point",
    [
        None,
        {},
        [],
        [1],
        [1, 2, 3],
        [True, 12],
        [34, False],
        ["34", 12],
        [float("nan"), 12],
        [34, float("inf")],
        [181, 12],
        [34, 91],
        [-181, 12],
        [34, -91],
        [10**1000, 12],
        (40, 30),
    ],
)
def test_invalid_pickup_points(point):
    assert pickup_coordinates({"venue_coordinates": point}) is None


def test_pickup_coordinate_order_and_zero():
    assert pickup_coordinates({"venue_coordinates": [40, 30]}) == (30.0, 40.0)
    assert pickup_coordinates({"venue_coordinates": [0, 0]}) == (0.0, 0.0)
