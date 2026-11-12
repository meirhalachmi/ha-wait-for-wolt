"""Synthetic fixtures matching audited field shapes, never live payloads."""

from decimal import Decimal

import pytest

from custom_components.wait_for_wolt.facts import (
    dropoff_coordinates,
    item_count,
    money,
    summary_total,
)
from custom_components.wait_for_wolt.privacy import is_purchase_id


@pytest.mark.parametrize("value", [True, -1, 1.5, "100", None, {}, []])
def test_money_rejects_ambiguous_or_malformed_amounts(value):
    assert money({"currency": "ILS", "price": value}, "price")[0] is None


@pytest.mark.parametrize(
    "currency,amount,expected",
    [("ILS", 1234, "12.34"), ("JPY", 1234, "1234"), ("KWD", 1234, "1.234")],
)
def test_explicit_minor_units(currency, amount, expected):
    assert money({"currency": currency, "price": amount}, "price") == (
        Decimal(expected),
        currency,
    )


def test_unknown_currency_and_ambiguous_summary_are_unknown():
    assert money({"currency": "XXX", "price": 10}, "price") == (None, None)
    assert summary_total({"total": "$10.00", "telemetry": {"end_amount": 1000}}) == (
        None,
        None,
    )
    assert (
        summary_total({"total": "₪10.00", "telemetry": {"end_amount": 900}})[0] is None
    )
    assert summary_total(
        {"total": "€1,234.56", "telemetry": {"end_amount": 123456}}
    ) == (Decimal("1234.56"), "EUR")


@pytest.mark.parametrize(
    "items,expected",
    [
        ([], 0),
        ([{"count": 2}, {"count": 3}], 5),
        ([{"count": True}], None),
        ([{"count": -1}], None),
        ([{}], None),
        (None, None),
    ],
)
def test_item_quantities(items, expected):
    assert item_count({"items": items}) == expected


@pytest.mark.parametrize(
    "point",
    [
        None,
        {"type": "LineString", "coordinates": [34, 12]},
        {"type": "Point", "coordinates": [True, 12]},
        {"type": "Point", "coordinates": [34, 91]},
        {"type": "Point", "coordinates": [float("nan"), 12]},
        {"type": "Point", "coordinates": [34, 12, 1]},
    ],
)
def test_bad_geojson_is_not_a_destination(point):
    assert dropoff_coordinates({"delivery_location": {"coordinates": point}}) is None


def test_only_canonical_registry_identities_are_purchase_candidates():
    assert is_purchase_id("a" * 24)
    for value in ("configuration", "a" * 23, "z" * 24, None):
        assert not is_purchase_id(value)
