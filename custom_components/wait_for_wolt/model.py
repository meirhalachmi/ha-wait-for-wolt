"""Per-order views of active purchases and the account-wide aggregate.

The fixed entities publish this aggregate instead of creating a device and
entities per purchase. Never publish raw orders, addresses, items, driver
identities or payments; coordinates leave this module only when the user has
enabled maps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util.location import distance

from .facts import item_count, summary_total, venue_name
from .status import extract_order_eta, normalize_order_status

# Progress order of the non-terminal states; a higher rank wins.
STATUS_RANK = {
    "unknown": 0,
    "pending": 1,
    "preparing": 2,
    "ready_for_pickup": 3,
    "picked_up": 4,
    "on_the_way": 5,
    "arriving": 6,
    "delivered": 7,
    "cancelled": 7,
    "failed": 7,
}
# Courier within this distance of the venue counts as "at the venue".
AT_VENUE_KM = 0.25
# Courier that was at the venue and is now farther than this has left with it.
LEFT_VENUE_KM = 0.4
# Courier this close to home is arriving even when Wolt still says otherwise.
ARRIVING_KM = 0.2
ARRIVAL_EVENTS = {"dropoff_arrival", "dropoff_started"}


def coordinates(lat: Any, lon: Any) -> dict[str, float]:
    """Reject malformed, nonfinite, and out-of-range positions."""
    if isinstance(lat, bool) or isinstance(lon, bool):
        return {}
    try:
        latitude, longitude = float(lat), float(lon)
    except TypeError, ValueError, OverflowError:
        return {}
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        return {}
    return {"latitude": latitude, "longitude": longitude}


def driver_fields(details: dict[str, Any]) -> dict[str, Any]:
    """Select the assigned driver, never an arbitrary other delivery driver."""
    drivers = details.get("_drivers")
    if not isinstance(drivers, list):
        return {}
    valid = [driver for driver in drivers if isinstance(driver, dict)]
    driver = next(
        (driver for driver in valid if driver.get("delivering_your_order") is True),
        None,
    )
    if (
        driver is None
        and len(valid) == 1
        and valid[0].get("delivering_your_order") is not False
    ):
        driver = valid[0]
    if driver is None:
        return {}
    out: dict[str, Any] = {}
    loc = driver.get("location")
    if isinstance(loc, list) and len(loc) == 2:
        out.update(coordinates(loc[1], loc[0]))
    if isinstance(driver.get("delivering_your_order"), bool):
        out["delivering_your_order"] = driver["delivering_your_order"]
    # ETA is parsed before entity publication; never expose its raw envelope.
    out["delivery_eta"] = driver.get("delivery_eta")
    heading = driver.get("heading")
    if (
        isinstance(heading, (int, float))
        and not isinstance(heading, bool)
        and math.isfinite(heading)
        and 0 <= heading <= 360
    ):
        out["courier_heading"] = heading
    return out


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    """Great-circle distance in kilometres."""
    meters = distance(a[0], a[1], b[0], b[1])
    return None if meters is None else meters / 1000


@dataclass(frozen=True, slots=True)
class OrderView:
    """What the fixed entities may say about one active purchase."""

    order_id: str
    venue: str | None
    status: str
    eta: datetime | None
    item_count: int | None
    total: str | None
    courier: tuple[float, float] | None
    courier_heading: float | None
    courier_distance_km: float | None
    pickup: tuple[float, float] | None

    def as_attribute(self) -> dict[str, Any]:
        """Scalar summary for the active-orders attribute list."""
        out: dict[str, Any] = {"order_id": self.order_id, "status": self.status}
        if self.venue:
            out["venue"] = self.venue
        if self.eta is not None:
            out["eta"] = self.eta.isoformat()
        if self.item_count is not None:
            out["item_count"] = self.item_count
        if self.total is not None:
            out["total"] = self.total
        if self.courier_distance_km is not None:
            out["courier_distance_km"] = round(self.courier_distance_km, 2)
        return out


def order_status(
    summary: dict[str, Any],
    details: dict[str, Any],
    *,
    left_venue: bool = False,
    courier_distance_km: float | None = None,
) -> str:
    """Most advanced status any source supports.

    The order list keeps saying "In progress" for the whole ride, so the rich
    details status and the courier's movement may move it forward. A later
    source never moves it back.
    """
    candidates = [normalize_order_status({**details, **summary})]
    if "status" in details:
        detail_status = normalize_order_status({"status": details["status"]})
        # Only the order list may end a purchase; stale detail text must not.
        if STATUS_RANK[detail_status] < STATUS_RANK["delivered"]:
            candidates.append(detail_status)
    if left_venue:
        candidates.append("on_the_way")
    if details.get("tracking_event_type") in ARRIVAL_EVENTS or (
        courier_distance_km is not None and courier_distance_km <= ARRIVING_KM
    ):
        candidates.append("arriving")
    return max(candidates, key=lambda status: STATUS_RANK[status])


def build_view(
    order_id: str,
    summary: dict[str, Any],
    details: dict[str, Any],
    *,
    pickup: dict[str, Any] | None,
    home: tuple[float, float],
    left_venue: bool,
    allow_location: bool,
) -> OrderView:
    """Project one active purchase onto the publishable view."""
    driver = driver_fields(details)
    courier = (
        (driver["latitude"], driver["longitude"]) if "latitude" in driver else None
    )
    courier_km = km_between(courier, home) if courier is not None else None
    point = coordinates((pickup or {}).get("latitude"), (pickup or {}).get("longitude"))
    total, currency = summary_total(summary)
    return OrderView(
        order_id=order_id,
        venue=venue_name(summary),
        status=order_status(
            summary,
            details,
            left_venue=left_venue,
            courier_distance_km=courier_km,
        ),
        eta=extract_order_eta(driver) or extract_order_eta({**details, **summary}),
        item_count=item_count(summary),
        total=f"{total} {currency}" if total is not None else None,
        courier=courier if allow_location else None,
        courier_heading=driver.get("courier_heading") if allow_location else None,
        courier_distance_km=courier_km if allow_location else None,
        pickup=(point["latitude"], point["longitude"])
        if point and allow_location
        else None,
    )


def lead_order(views: list[OrderView]) -> OrderView | None:
    """The purchase that arrives first: earliest ETA, then the most advanced."""
    if not views:
        return None
    return min(
        views,
        key=lambda view: (
            view.eta is None,
            view.eta.timestamp() if view.eta is not None else 0,
            -STATUS_RANK[view.status],
            view.order_id,
        ),
    )
