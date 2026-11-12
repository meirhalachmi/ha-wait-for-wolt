"""Tests for Wolt order and venue sensor behavior."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wait_for_wolt.api import WoltApi, WoltConnectionError
from custom_components.wait_for_wolt.const import (
    CONF_BEARER_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    CONF_VENUE_IDS,
    DOMAIN,
)
from custom_components.wait_for_wolt.coordinator import (
    WoltCoordinatorData,
    WoltDataUpdateCoordinator,
    WoltRuntimeData,
)
from custom_components.wait_for_wolt.sensor import (
    WoltVenueSensor,
    async_setup_entry,
    async_setup_platform,
    extract_order_eta,
    normalize_order_status,
)


def load_json_fixture(name: str) -> Any:
    """Load a sanitized fixture."""
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def mock_coordinator(data: WoltCoordinatorData) -> Mock:
    """Create the coordinator surface consumed by entities and setup."""
    coordinator = Mock(spec=WoltDataUpdateCoordinator)
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.async_add_listener.return_value = Mock()
    return coordinator


async def test_legacy_yaml_platform_starts_config_entry_import(
    hass: HomeAssistant,
) -> None:
    """Migrate YAML credentials instead of running with non-durable rotation."""
    config = {
        CONF_NAME: "Sanitized Wolt",
        CONF_SESSION_ID: "sanitized-session-id",
        CONF_BEARER_TOKEN: "sanitized-access-token",
        CONF_REFRESH_TOKEN: "sanitized-refresh-token",
        CONF_VENUE_IDS: ["sanitized-venue"],
    }

    with patch.object(
        hass.config_entries.flow,
        "async_init",
        AsyncMock(return_value={}),
    ) as import_flow:
        await async_setup_platform(hass, config, Mock())

    import_flow.assert_awaited_once_with(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data=config,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Preparing your order", "preparing"),
        ("READY_FOR_PICKUP", "ready_for_pickup"),
        ("Courier picked up", "picked_up"),
        ("On the way", "on_the_way"),
        ("Courier nearby", "arriving"),
        ("CANCELLED", "cancelled"),
        ("REJECTED", "failed"),
        ("delivery pending", "pending"),
        ("delivery pickup", "picked_up"),
        ("preparing not delivered", "preparing"),
        ("not cancelled", "unknown"),
        ("not yet delivered", "unknown"),
        ("delivery not yet completed", "on_the_way"),
        ("not currently ready", "unknown"),
        ("undelivered", "unknown"),
        ("new private state", "unknown"),
    ],
)
def test_order_status_normalization_is_stable(raw: str, expected: str) -> None:
    """Keep automations stable when Wolt changes display text."""
    assert normalize_order_status({"status": {"value": raw}}) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2030-01-01T12:30:00Z", datetime(2030, 1, 1, 12, 30, tzinfo=UTC)),
        (1893501000000, datetime(2030, 1, 1, 12, 30, tzinfo=UTC)),
        (35, None),
        (0, None),
        (-1, None),
        ({"min": 25, "max": 35}, None),
        (True, None),
        ("25-35 min", None),
        (None, None),
    ],
)
def test_order_eta_requires_an_explicit_timestamp(value: Any, expected: Any) -> None:
    """Never guess a timestamp from Wolt's human-readable duration text."""
    assert extract_order_eta({"delivery_eta": value}) == expected


async def test_legacy_venue_unique_id_migrates_to_config_entry_scope(
    hass: HomeAssistant,
) -> None:
    """Preserve venue registry customizations while enabling multiple accounts."""
    slug = "sanitized-venue"
    coordinator = mock_coordinator(WoltCoordinatorData({}, frozenset(), {}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NAME: "Sanitized Wolt", CONF_VENUE_IDS: [slug]},
    )
    api = AsyncMock(spec=WoltApi)
    entry.runtime_data = WoltRuntimeData(api, coordinator)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"wolt_venue_{slug}",
        config_entry=entry,
        suggested_object_id="favorite_place",
    )
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    migrated = registry.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{entry.entry_id}_venue_{slug}"
    assert migrated.entity_id == "sensor.favorite_place"
    venue_call = add_entities.call_args_list[0]
    venue = venue_call.args[0][0]
    assert venue.unique_id == migrated.unique_id
    assert venue_call.kwargs == {"update_before_add": True}


@pytest.mark.parametrize(
    ("fixture_name", "expected_state"),
    [("venue_open.json", "open"), ("venue_closed.json", "closed")],
)
async def test_venue_sensor_state_and_availability(
    fixture_name: str,
    expected_state: str,
) -> None:
    """Handle open and explicitly closed venues without assuming metadata exists."""
    api = AsyncMock(spec=WoltApi)
    api.fetch_venue_details.return_value = load_json_fixture(fixture_name)
    sensor = WoltVenueSensor(
        api, "entry-001", "sanitized-venue", "Wolt sanitized-venue"
    )

    await sensor.async_update()

    assert sensor.unique_id == "entry-001_venue_sanitized-venue"
    assert sensor.native_value == expected_state
    assert sensor.available

    api.fetch_venue_details.side_effect = WoltConnectionError("offline")
    await sensor.async_update()

    assert not sensor.available


async def test_venue_sensor_respects_explicit_closed_status() -> None:
    """Prefer an explicit closed status over broader online metadata."""
    api = AsyncMock(spec=WoltApi)
    api.fetch_venue_details.return_value = {
        "venue": {
            "online": True,
            "delivery_open_status": {"is_open": False},
        }
    }
    sensor = WoltVenueSensor(
        api, "entry-001", "sanitized-venue", "Wolt sanitized-venue"
    )

    await sensor.async_update()

    assert sensor.available
    assert sensor.native_value == "closed"


async def test_venue_missing_availability_is_unknown() -> None:
    api = AsyncMock(spec=WoltApi)
    api.fetch_venue_details.return_value = {"venue": {"name": "synthetic"}}
    sensor = WoltVenueSensor(api, "entry-001", "synthetic", "Wolt synthetic")
    await sensor.async_update()
    assert sensor.available
    assert sensor.native_value is None
