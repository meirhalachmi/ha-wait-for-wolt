"""Fixed account sensors, dynamic map points, and removal of per-order devices."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wait_for_wolt import async_remove_config_entry_device
from custom_components.wait_for_wolt.api import WoltApi, WoltConnectionError
from custom_components.wait_for_wolt.const import DOMAIN
from custom_components.wait_for_wolt.model import order_status

HOME = (32.0, 34.8)
VENUE_A = {"latitude": 32.01, "longitude": 34.8}  # ~1.1 km north of home
VENUE_B = {"latitude": 31.99, "longitude": 34.8}  # ~1.1 km south of home
A, B = "a" * 24, "b" * 24


def summary(order_id: str, venue: str, status: str = "IN_PROGRESS") -> dict:
    return {
        "purchase_id": order_id,
        "status": {"value": "In progress"},
        "telemetry": {"order_status_type": status},
        "venue": {"name": venue},
    }


def details(eta_minutes: int, courier: tuple[float, float] | None = None) -> dict:
    eta = (dt_util.utcnow() + timedelta(minutes=eta_minutes)).isoformat()
    out: dict = {"client_pre_estimate": {"delivery_eta": {"$date": eta}}}
    if courier is not None:
        out["_drivers"] = [
            {
                "location": [courier[1], courier[0]],
                "delivering_your_order": True,
                "name": "never exposed",
            }
        ]
    return out


class FakeWolt:
    """Mutable account state behind the patched API."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.details: dict[str, dict] = {}
        self.venues: dict[str, dict] = {}

    async def fetch_orders(self) -> list[dict]:
        return self.orders

    async def fetch_order_details(self, order_id: str) -> dict:
        return self.details.get(order_id, {})

    async def fetch_venue_location(self, order: dict) -> dict:
        return self.venues.get(order["purchase_id"], {})


@pytest.fixture
def wolt():
    fake = FakeWolt()
    with (
        patch.object(WoltApi, "fetch_orders", AsyncMock(side_effect=fake.fetch_orders)),
        patch.object(
            WoltApi,
            "fetch_order_details",
            AsyncMock(side_effect=fake.fetch_order_details),
        ),
        patch.object(
            WoltApi,
            "fetch_venue_location",
            AsyncMock(side_effect=fake.fetch_venue_location),
        ),
    ):
        yield fake


async def setup(hass: HomeAssistant, maps: bool = True) -> MockConfigEntry:
    hass.config.latitude, hass.config.longitude = HOME
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"bearer_token": "access", "refresh_token": "refresh", "venue_ids": []},
        options={"tracking_maps": maps},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def points(hass: HomeAssistant) -> dict[tuple[str, str], object]:
    return {
        (state.attributes["order_id"], state.attributes["kind"]): state
        for state in hass.states.async_all("geo_location")
        if state.attributes.get("source") == DOMAIN
    }


async def test_idle_account_has_one_device_and_fixed_sensors(hass, wolt):
    entry = await setup(hass)

    assert hass.states.get("sensor.wolt_status").state == "none"
    assert hass.states.get("sensor.wolt_active_orders").state == "0"
    assert hass.states.get("sensor.wolt_eta").state == "unknown"
    assert hass.states.get("sensor.wolt_courier_distance").state == "unknown"
    assert points(hass) == {}
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Wolt"]


async def test_one_order_rides_home_and_leaves_nothing_behind(hass, wolt):
    entry = await setup(hass)
    wolt.orders = [summary(A, "Pizza")]
    wolt.details = {A: details(30)}
    wolt.venues = {A: VENUE_A}
    await refresh(hass, entry)

    assert hass.states.get("sensor.wolt_status").state == "pending"
    assert hass.states.get("sensor.wolt_status").attributes["venue"] == "Pizza"
    assert hass.states.get("sensor.wolt_active_orders").state == "1"
    assert set(points(hass)) == {(A, "venue")}

    # Courier reaches the venue: still pending, now on the map.
    wolt.details = {A: details(20, courier=(32.0101, 34.8))}
    await refresh(hass, entry)
    assert hass.states.get("sensor.wolt_status").state == "pending"
    assert set(points(hass)) == {(A, "venue"), (A, "courier")}
    courier = points(hass)[(A, "courier")]
    assert courier.attributes["venue"] == "Pizza"
    assert "never exposed" not in str(courier.attributes)

    # Courier leaves the venue: on the way even though Wolt still says pending.
    wolt.details = {A: details(10, courier=(32.005, 34.8))}
    await refresh(hass, entry)
    assert hass.states.get("sensor.wolt_status").state == "on_the_way"
    distance = float(hass.states.get("sensor.wolt_courier_distance").state)
    assert 0.5 < distance < 0.6
    assert float(points(hass)[(A, "courier")].state) == pytest.approx(
        distance, abs=0.06
    )

    # At the door.
    wolt.details = {A: details(1, courier=(32.001, 34.8))}
    await refresh(hass, entry)
    assert hass.states.get("sensor.wolt_status").state == "arriving"

    wolt.orders = [summary(A, "Pizza", "DELIVERED")]
    await refresh(hass, entry)
    assert hass.states.get("sensor.wolt_status").state == "none"
    assert hass.states.get("sensor.wolt_active_orders").state == "0"
    assert hass.states.get("sensor.wolt_courier_distance").state == "unknown"
    assert points(hass) == {}
    registry = er.async_get(hass)
    assert not [e for e in registry.entities.values() if e.domain == "geo_location"], (
        "map points must never enter the entity registry"
    )
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1


async def test_two_orders_aggregate_on_the_first_to_arrive(hass, wolt):
    entry = await setup(hass)
    wolt.orders = [summary(A, "Pizza"), summary(B, "Sushi")]
    wolt.details = {
        A: details(25, courier=(32.008, 34.8)),
        B: details(12, courier=(31.995, 34.8)),
    }
    wolt.venues = {A: VENUE_A, B: VENUE_B}
    await refresh(hass, entry)

    status = hass.states.get("sensor.wolt_status")
    assert status.attributes["order_id"] == B
    assert status.attributes["venue"] == "Sushi"
    assert status.attributes["venues"] == "Pizza + Sushi"
    eta = dt_util.parse_datetime(hass.states.get("sensor.wolt_eta").state)
    assert timedelta(minutes=11) < eta - dt_util.utcnow() < timedelta(minutes=13)
    orders = hass.states.get("sensor.wolt_active_orders").attributes["orders"]
    assert [o["order_id"] for o in orders] == [B, A]
    assert hass.states.get("sensor.wolt_courier_distance").attributes["venue"] == (
        "Sushi"
    )
    assert {kind for _, kind in points(hass)} == {"courier", "venue"}
    assert len(points(hass)) == 4

    # Sushi arrives; everything moves to the pizza.
    wolt.orders = [summary(A, "Pizza"), summary(B, "Sushi", "DELIVERED")]
    await refresh(hass, entry)
    assert hass.states.get("sensor.wolt_status").attributes["venue"] == "Pizza"
    assert hass.states.get("sensor.wolt_active_orders").state == "1"
    assert set(points(hass)) == {(A, "courier"), (A, "venue")}


async def test_without_maps_no_positions_leave_the_integration(hass, wolt):
    entry = await setup(hass, maps=False)
    wolt.orders = [summary(A, "Pizza")]
    wolt.details = {A: details(10, courier=(32.001, 34.8))}
    await refresh(hass, entry)

    assert hass.states.get("sensor.wolt_status").state == "arriving"
    assert hass.states.get("sensor.wolt_courier_distance").state == "unknown"
    orders = hass.states.get("sensor.wolt_active_orders").attributes["orders"]
    assert "courier_distance_km" not in orders[0]
    assert points(hass) == {}


async def test_per_order_devices_of_earlier_versions_are_removed(hass, wolt):
    hass.config.latitude, hass.config.longitude = HOME
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"bearer_token": "a", "refresh_token": "r", "venue_ids": []},
        options={"tracking_maps": True},
    )
    entry.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    old = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:{A}")},
        name="Wolt order",
    )
    for key in ("status", "eta", "delivery", "item_count"):
        entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{A}_{key}",
            config_entry=entry,
            device_id=old.id,
        )
    venue = entities.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_venue_pizza", config_entry=entry
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert devices.async_get(old.id) is None
    left = {
        e.unique_id for e in er.async_entries_for_config_entry(entities, entry.entry_id)
    }
    assert left == {
        venue.unique_id,
        *(
            f"{entry.entry_id}_account_{k}"
            for k in ("status", "eta", "active_orders", "courier_distance")
        ),
    }
    account = dr.async_entries_for_config_entry(devices, entry.entry_id)[0]
    assert not await async_remove_config_entry_device(hass, entry, account)
    stray = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "stray")}
    )
    assert await async_remove_config_entry_device(hass, entry, stray)


@pytest.mark.parametrize(
    ("summary_status", "detail", "expected"),
    [
        # The order list says "In progress" all ride; details may know more.
        ({"value": "In progress"}, {"status": "delivery"}, "on_the_way"),
        ({"value": "In progress"}, {"status": "acknowledged"}, "preparing"),
        # Stale detail text never ends an active order.
        ({"value": "In progress"}, {"status": "Delivered"}, "pending"),
        (
            {"value": "In progress"},
            {"tracking_event_type": "dropoff_arrival"},
            "arriving",
        ),
        # A later source never moves the status back.
        ({"value": "Courier nearby"}, {"status": "production"}, "arriving"),
    ],
)
def test_status_takes_the_most_advanced_source(summary_status, detail, expected):
    order = {
        "status": summary_status,
        "telemetry": {"order_status_type": "IN_PROGRESS"},
    }
    assert order_status(order, detail) == expected


async def test_failed_details_call_keeps_the_courier_on_the_map(hass, wolt):
    entry = await setup(hass)
    wolt.orders = [summary(A, "Pizza")]
    wolt.details = {A: details(10, courier=(32.005, 34.8))}
    await refresh(hass, entry)
    assert (A, "courier") in points(hass)

    with patch.object(
        WoltApi,
        "fetch_order_details",
        AsyncMock(side_effect=WoltConnectionError("down")),
    ):
        await refresh(hass, entry)
    assert (A, "courier") in points(hass)
    assert hass.states.get("sensor.wolt_courier_distance").state != "unknown"
