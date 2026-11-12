"""Coordinator for conservative Wolt order polling."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    WoltApi,
    WoltAuthenticationError,
    WoltConnectionError,
    WoltInvalidPayloadError,
    WoltRateLimitError,
    is_active_order,
)
from .const import DOMAIN
from .facts import pickup_coordinates
from .model import (
    AT_VENUE_KM,
    LEFT_VENUE_KM,
    OrderView,
    build_view,
    coordinates,
    driver_fields,
    km_between,
)
from .privacy import location_allowed, location_enabled

_LOGGER = logging.getLogger(__name__)

ACTIVE_UPDATE_INTERVAL = timedelta(seconds=30)
IDLE_UPDATE_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class WoltCoordinatorData:
    """One coherent snapshot of account orders and active-order details."""

    orders: dict[str, dict[str, Any]]
    active_order_ids: frozenset[str]
    details: dict[str, dict[str, Any]]
    pickups: dict[str, dict[str, Any]] = field(default_factory=dict)
    views: dict[str, OrderView] = field(default_factory=dict)


class WoltDataUpdateCoordinator(DataUpdateCoordinator[WoltCoordinatorData]):
    """Fetch each authenticated Wolt resource once per polling cycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WoltApi,
    ) -> None:
        """Initialize the coordinator at the conservative idle interval."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=IDLE_UPDATE_INTERVAL,
        )
        self.api = api
        self.entry = entry
        self._rich_retry: dict[str, tuple[float, int]] = {}
        self._rich_tracking_warning_logged = False
        # Purchases whose courier was seen at the venue, and those that left it.
        self._at_venue: set[str] = set()
        self._left_venue: set[str] = set()
        # Last rich details per active purchase, so a skipped or failed details
        # call does not make the courier vanish from the map for a cycle.
        self._last_details: dict[str, dict[str, Any]] = {}

    async def _async_update_data(self) -> WoltCoordinatorData:
        """Fetch orders and details, translating failures for Home Assistant."""
        try:
            raw_orders = await self.api.fetch_orders()
            orders = {
                order_id: order
                for order in raw_orders
                if (order_id := self.order_id(order)) is not None
            }
            active_order_ids = frozenset(
                order_id for order_id, order in orders.items() if is_active_order(order)
            )
            details: dict[str, dict[str, Any]] = {}
            pickups: dict[str, dict[str, Any]] = {}
            rich_tracking_failed = False
            # Orders are normally singular. Keep requests sequential to avoid bursts
            # against Wolt's unofficial consumer endpoints.
            self._rich_retry = {
                key: value
                for key, value in self._rich_retry.items()
                if key in active_order_ids
            }
            for order_id in sorted(active_order_ids):
                try:
                    retry_at, failures = self._rich_retry.get(order_id, (0, 0))
                    if monotonic() < retry_at:
                        rich_tracking_failed = True
                    else:
                        details[order_id] = await self.api.fetch_order_details(order_id)
                        self._rich_retry.pop(order_id, None)
                except (
                    WoltAuthenticationError,
                    WoltRateLimitError,
                    WoltConnectionError,
                    WoltInvalidPayloadError,
                ):
                    # The summary remains useful while the optional rich endpoint
                    # is unavailable, forbidden, limited, or has not populated a
                    # newly placed order. Authentication for the primary orders
                    # endpoint remains authoritative and is handled below.
                    rich_tracking_failed = True
                    self._rich_retry[order_id] = (
                        monotonic() + min(60 * 2 ** min(failures, 4), 900),
                        failures + 1,
                    )
                if location_enabled(self.hass, self.entry, order_id, "pickup"):
                    point = pickup_coordinates(details.get(order_id, {}))
                    if point is not None:
                        pickups[order_id] = {
                            "latitude": point[0],
                            "longitude": point[1],
                            "coordinate_source": "wolt_order_details",
                        }
                    else:
                        with suppress(WoltConnectionError, WoltInvalidPayloadError):
                            pickups[order_id] = await self.api.fetch_venue_location(
                                orders[order_id]
                            )
            if rich_tracking_failed and not self._rich_tracking_warning_logged:
                _LOGGER.warning("Rich Wolt order tracking details are unavailable")
            self._rich_tracking_warning_logged = rich_tracking_failed
        except WoltAuthenticationError as err:
            raise ConfigEntryAuthFailed("Wolt authentication failed") from err
        except WoltRateLimitError as err:
            raise UpdateFailed("Wolt rate limit reached") from err
        except (WoltConnectionError, WoltInvalidPayloadError) as err:
            raise UpdateFailed("Unable to update Wolt orders") from err

        self.update_interval = (
            ACTIVE_UPDATE_INTERVAL if active_order_ids else IDLE_UPDATE_INTERVAL
        )
        return WoltCoordinatorData(
            orders,
            active_order_ids,
            details,
            pickups,
            self._build_views(orders, active_order_ids, details, pickups),
        )

    def _build_views(
        self,
        orders: dict[str, dict[str, Any]],
        active_order_ids: frozenset[str],
        details: dict[str, dict[str, Any]],
        pickups: dict[str, dict[str, Any]],
    ) -> dict[str, OrderView]:
        """Project active purchases, remembering when a courier left the venue."""
        self._at_venue &= active_order_ids
        self._left_venue &= active_order_ids
        self._last_details = {
            order_id: details.get(order_id, self._last_details.get(order_id, {}))
            for order_id in active_order_ids
        }
        home = (self.hass.config.latitude, self.hass.config.longitude)
        views: dict[str, OrderView] = {}
        for order_id in sorted(active_order_ids):
            detail = self._last_details[order_id]
            driver = driver_fields(detail)
            venue = coordinates(
                pickups.get(order_id, {}).get("latitude"),
                pickups.get(order_id, {}).get("longitude"),
            )
            if "latitude" in driver and venue:
                km = km_between(
                    (driver["latitude"], driver["longitude"]),
                    (venue["latitude"], venue["longitude"]),
                )
                if km is not None and km <= AT_VENUE_KM:
                    self._at_venue.add(order_id)
                elif (
                    km is not None and km > LEFT_VENUE_KM and order_id in self._at_venue
                ):
                    self._left_venue.add(order_id)
            views[order_id] = build_view(
                order_id,
                orders[order_id],
                detail,
                pickup=pickups.get(order_id),
                home=home,
                left_venue=order_id in self._left_venue,
                allow_location=location_allowed(self.entry, order_id, "delivery"),
            )
        return views

    @staticmethod
    def order_id(order: dict[str, Any]) -> str | None:
        """Return Wolt's current purchase ID with legacy fallbacks."""
        value = order.get("purchase_id") or order.get("order_id") or order.get("id")
        return str(value) if value else None


@dataclass(slots=True)
class WoltRuntimeData:
    """Runtime objects owned by one Wolt config entry."""

    api: WoltApi
    coordinator: WoltDataUpdateCoordinator
