"""Sensor platform for Wolt order tracker.

One fixed set of account-level sensors summarizes every active purchase, so a
purchase never creates its own device or entities.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import WoltApi, WoltApiError
from .const import (
    CONF_BEARER_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    CONF_VENUE_IDS,
    DEFAULT_NAME,
    DOMAIN,
)
from .coordinator import WoltDataUpdateCoordinator
from .model import OrderView, lead_order
from .status import ORDER_STATUS_OPTIONS
from .status import _parse_eta as _parse_eta  # noqa: F401  (re-export)
from .status import extract_order_eta as extract_order_eta  # noqa: F401
from .status import normalize_order_status as normalize_order_status  # noqa: F401

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)

NO_ORDER = "none"
ACCOUNT_SENSOR_KEYS = ("status", "eta", "active_orders", "courier_distance")

STATUS_DESCRIPTION = SensorEntityDescription(
    key="status",
    translation_key="status",
    device_class=SensorDeviceClass.ENUM,
    options=[NO_ORDER, *ORDER_STATUS_OPTIONS],
    icon="mdi:moped",
)
ETA_DESCRIPTION = SensorEntityDescription(
    key="eta",
    translation_key="eta",
    device_class=SensorDeviceClass.TIMESTAMP,
    icon="mdi:clock-outline",
)
ACTIVE_ORDERS_DESCRIPTION = SensorEntityDescription(
    key="active_orders",
    translation_key="active_orders",
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:package-variant",
)
COURIER_DISTANCE_DESCRIPTION = SensorEntityDescription(
    key="courier_distance",
    translation_key="courier_distance",
    device_class=SensorDeviceClass.DISTANCE,
    native_unit_of_measurement=UnitOfLength.KILOMETERS,
    suggested_display_precision=2,
    icon="mdi:map-marker-distance",
)


PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_SESSION_ID, default=""): cv.string,
        vol.Required(CONF_BEARER_TOKEN): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        vol.Optional(CONF_VENUE_IDS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Import legacy YAML into a config entry with durable token rotation."""
    del async_add_entities, discovery_info
    _LOGGER.warning(
        "YAML configuration for wait_for_wolt is deprecated; importing it into "
        "the Home Assistant integration UI. Remove the YAML after import"
    )
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_NAME: config[CONF_NAME],
            CONF_SESSION_ID: config.get(CONF_SESSION_ID, ""),
            CONF_BEARER_TOKEN: config[CONF_BEARER_TOKEN],
            CONF_REFRESH_TOKEN: config[CONF_REFRESH_TOKEN],
            CONF_VENUE_IDS: config.get(CONF_VENUE_IDS, []),
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fixed account sensors and public venue sensors."""
    data = {**entry.data, **entry.options}
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api
    name = data.get(CONF_NAME, DEFAULT_NAME)
    venues = data.get(CONF_VENUE_IDS, [])
    if venues:
        registry = er.async_get(hass)
        for slug in venues:
            scoped_unique_id = _venue_unique_id(entry.entry_id, slug)
            legacy_entity = _owned_registry_entity(
                registry,
                entry.entry_id,
                f"wolt_venue_{slug}",
            )
            if (
                legacy_entity is not None
                and registry.async_get_entity_id("sensor", DOMAIN, scoped_unique_id)
                is None
            ):
                registry.async_update_entity(
                    legacy_entity.entity_id,
                    new_unique_id=scoped_unique_id,
                )
        async_add_entities(
            [
                WoltVenueSensor(api, entry.entry_id, slug, f"{name} {slug}")
                for slug in venues
            ],
            update_before_add=True,
        )

    async_add_entities(
        [
            WoltStatusSensor(coordinator, entry.entry_id),
            WoltEtaSensor(coordinator, entry.entry_id),
            WoltActiveOrdersSensor(coordinator, entry.entry_id),
            WoltCourierDistanceSensor(coordinator, entry.entry_id),
        ]
    )


def account_device_info(entry_id: str) -> DeviceInfo:
    """The one device every Wolt entity of an account belongs to."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        manufacturer="Wolt",
        model="Account",
        name="Wolt",
        entry_type=DeviceEntryType.SERVICE,
    )


def account_unique_id(entry_id: str, key: str) -> str:
    """Scope account sensors to one config entry."""
    return f"{entry_id}_account_{key}"


def _owned_registry_entity(
    registry: er.EntityRegistry,
    entry_id: str,
    unique_id: str,
) -> er.RegistryEntry | None:
    """Return a sensor registry entity only when this config entry owns it."""
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    entity = registry.async_get(entity_id) if entity_id is not None else None
    return entity if entity is not None and entity.config_entry_id == entry_id else None


def _venue_unique_id(entry_id: str, slug: str) -> str:
    """Scope venue entities to one config entry."""
    return f"{entry_id}_venue_{slug}"


class WoltAccountSensor(CoordinatorEntity[WoltDataUpdateCoordinator], SensorEntity):
    """Base for a fixed sensor that summarizes all active purchases."""

    _attr_attribution = "Data provided by Wolt"
    _attr_has_entity_name = True

    def __init__(self, coordinator: WoltDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = account_unique_id(entry_id, self.entity_description.key)
        self._attr_device_info = account_device_info(entry_id)

    @property
    def _views(self) -> list[OrderView]:
        return list(self.coordinator.data.views.values())

    @property
    def _lead(self) -> OrderView | None:
        return lead_order(self._views)


class WoltStatusSensor(WoltAccountSensor):
    """Status of the purchase that arrives first, or none."""

    entity_description = STATUS_DESCRIPTION

    @property
    def native_value(self) -> str:
        lead = self._lead
        return lead.status if lead is not None else NO_ORDER

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead
        if lead is None:
            return {}
        attrs: dict[str, Any] = {"order_id": lead.order_id}
        venues = [view.venue for view in self._views if view.venue]
        if lead.venue:
            attrs["venue"] = lead.venue
        if venues:
            attrs["venues"] = " + ".join(venues)
        return attrs


class WoltEtaSensor(WoltAccountSensor):
    """Earliest estimated arrival among active purchases."""

    entity_description = ETA_DESCRIPTION

    @property
    def native_value(self) -> datetime | None:
        lead = self._lead
        return lead.eta if lead is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead
        if lead is None or lead.eta is None:
            return {}
        remaining = (lead.eta - dt_util.utcnow()).total_seconds()
        return {
            "order_id": lead.order_id,
            "minutes_to_arrival": max(0, round(remaining / 60)),
        }


class WoltActiveOrdersSensor(WoltAccountSensor):
    """Number of active purchases, with a scalar summary of each."""

    entity_description = ACTIVE_ORDERS_DESCRIPTION

    @property
    def native_value(self) -> int:
        return len(self._views)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead
        ordered = sorted(self._views, key=lambda view: view is not lead)
        return {"orders": [view.as_attribute() for view in ordered]}


class WoltCourierDistanceSensor(WoltAccountSensor):
    """Distance from home of the nearest courier carrying one of our orders."""

    entity_description = COURIER_DISTANCE_DESCRIPTION

    @property
    def _nearest(self) -> OrderView | None:
        located = [v for v in self._views if v.courier_distance_km is not None]
        return min(located, key=lambda v: v.courier_distance_km, default=None)

    @property
    def native_value(self) -> float | None:
        nearest = self._nearest
        return nearest.courier_distance_km if nearest is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nearest = self._nearest
        if nearest is None:
            return {}
        attrs: dict[str, Any] = {"order_id": nearest.order_id}
        if nearest.venue:
            attrs["venue"] = nearest.venue
        return attrs


class WoltVenueSensor(SensorEntity):
    """Sensor representing a Wolt venue's availability."""

    _attr_attribution = "Data provided by Wolt"

    def __init__(self, api: WoltApi, entry_id: str, slug: str, name: str) -> None:
        self.api = api
        self.slug = slug
        self._attr_name = name
        self._attr_unique_id = _venue_unique_id(entry_id, slug)
        self._state = None
        self._attr_extra_state_attributes = {}
        self._attr_available = False
        self._attr_icon = "mdi:store"

    @property
    def native_value(self):
        return self._state

    async def async_update(self) -> None:
        try:
            details = await self.api.fetch_venue_details(self.slug)
        except WoltApiError as err:
            self._attr_available = False
            _LOGGER.warning("Unable to update a configured Wolt venue: %s", err)
            return
        if not details:
            self._attr_available = False
            _LOGGER.warning("Configured Wolt venue details were not found")
            return

        venue = details.get("venue") or details.get("venue_info") or {}
        self._attr_available = True
        open_info = venue.get("delivery_open_status") or venue.get("open_status") or {}

        is_open = open_info.get("is_open")
        if is_open is None:
            is_open = venue.get("online")
        if is_open is None:
            is_open = venue.get("is_open")
        if venue.get("online") is False:
            is_open = False
        self._state = (
            "open" if is_open is True else "closed" if is_open is False else None
        )

        # Extract estimates for available delivery methods
        estimates: dict[str, Any] = {}
        for cfg in venue.get("delivery_configs", []):
            method = cfg.get("method")
            estimate = cfg.get("estimate") or {}
            if method and estimate:
                estimates[f"{method}_estimate_min"] = estimate.get("min")
                estimates[f"{method}_estimate_max"] = estimate.get("max")

        # Parse useful metadata from the header section
        header = venue.get("header", {})
        statuses = (
            header.get("delivery_method_statuses", [])
            if isinstance(header, dict)
            else []
        )
        meta = (
            statuses[0].get("metadata", [])
            if statuses and isinstance(statuses[0], dict)
            else []
        )
        rating = None
        delivery_fee = None
        service_fee = None
        min_order_text = None
        for item in meta:
            icon = item.get("icon")
            value = item.get("value")
            if icon and icon.startswith("RATING"):
                rating = value
            elif icon == "CYCLIST":
                delivery_fee = value
            elif value and "Min. order" in value:
                min_order_text = value
            elif value and "Service fee" in value:
                service_fee = value

        banner_text = None
        if venue.get("banners"):
            banner = venue["banners"][0]
            discount = banner.get("discount") or banner
            banner_text = discount.get("formatted_text")

        self._attr_extra_state_attributes = {
            "online": venue.get("online"),
            "open_status": open_info.get("value"),
            "next_open": open_info.get("next_open"),
            "next_close": open_info.get("next_close"),
            "order_minimum": details.get("order_minimum"),
            "is_venue_favourite": details.get("is_venue_favourite"),
            "rating": rating,
            "delivery_fee": delivery_fee,
            "service_fee": service_fee,
            "min_order_text": min_order_text,
            "discount": banner_text,
            **estimates,
        }
