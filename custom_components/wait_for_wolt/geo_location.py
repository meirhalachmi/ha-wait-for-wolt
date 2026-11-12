"""Map points that exist only while a purchase is on its way.

Each active purchase gets a courier point (once Wolt shares the courier's
position) and a venue point. They are removed when the purchase ends, and have
no unique ID, so nothing accumulates in the entity registry. A map card shows
them all with ``geo_location_sources: [wait_for_wolt]``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WoltDataUpdateCoordinator
from .model import OrderView, km_between

KINDS = ("courier", "venue")


class WoltMapPoint(CoordinatorEntity[WoltDataUpdateCoordinator], GeolocationEvent):
    """The courier or the venue of one active purchase."""

    _attr_attribution = "Data provided by Wolt"
    _attr_should_poll = False
    _attr_source = DOMAIN
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(
        self, coordinator: WoltDataUpdateCoordinator, order_id: str, kind: str
    ) -> None:
        super().__init__(coordinator)
        self.order_id = order_id
        self.kind = kind
        self._attr_icon = "mdi:moped" if kind == "courier" else "mdi:storefront"
        view = self._view
        label = view.venue if view is not None and view.venue else "order"
        self._attr_name = f"Wolt {kind} {label}"

    @property
    def _view(self) -> OrderView | None:
        return self.coordinator.data.views.get(self.order_id)

    @property
    def _point(self) -> tuple[float, float] | None:
        return point_of(self._view, self.kind)

    @property
    def latitude(self) -> float | None:
        point = self._point
        return point[0] if point is not None else None

    @property
    def longitude(self) -> float | None:
        point = self._point
        return point[1] if point is not None else None

    @property
    def distance(self) -> float | None:
        point = self._point
        if point is None:
            return None
        km = km_between(point, (self.hass.config.latitude, self.hass.config.longitude))
        return round(km, 2) if km is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        view = self._view
        attrs: dict[str, Any] = {"order_id": self.order_id, "kind": self.kind}
        if view is None:
            return attrs
        if view.venue:
            attrs["venue"] = view.venue
        attrs["order_status"] = view.status
        if self.kind == "courier" and view.courier_heading is not None:
            attrs["heading"] = view.courier_heading
        return attrs


def point_of(view: OrderView | None, kind: str) -> tuple[float, float] | None:
    """Where a map point of this kind is, if Wolt says."""
    if view is None:
        return None
    return view.courier if kind == "courier" else view.pickup


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add and remove map points as purchases start, move and end."""
    coordinator: WoltDataUpdateCoordinator = entry.runtime_data.coordinator
    points: dict[tuple[str, str], WoltMapPoint] = {}

    @callback
    def sync() -> None:
        wanted = {
            (order_id, kind)
            for order_id, view in coordinator.data.views.items()
            for kind in KINDS
            if point_of(view, kind) is not None
        }
        for key in set(points) - wanted:
            hass.async_create_task(points.pop(key).async_remove(force_remove=True))
        new = [WoltMapPoint(coordinator, *key) for key in wanted - set(points)]
        for point in new:
            points[(point.order_id, point.kind)] = point
        if new:
            async_add_entities(new)

    sync()
    entry.async_on_unload(coordinator.async_add_listener(sync))
