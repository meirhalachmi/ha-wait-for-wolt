"""Map Wolt's unstable status text and ETA shapes to fixed values."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.util import dt as dt_util

ORDER_STATUS_OPTIONS = [
    "pending",
    "preparing",
    "ready_for_pickup",
    "picked_up",
    "on_the_way",
    "arriving",
    "delivered",
    "cancelled",
    "failed",
    "unknown",
]


def _contains_non_negated_status(value: str, *fragments: str) -> bool:
    """Match fragments without treating negative status phrases as that state."""
    return any(
        fragment in value
        and re.search(
            rf"(?:^|_)(?:(?:not(?:_(?:yet|currently))?|non)_|un){re.escape(fragment)}",
            value,
        )
        is None
        for fragment in fragments
    )


def _raw_status(order: dict[str, Any]) -> str | None:
    """Extract a scalar status while respecting authoritative telemetry."""
    status_type: Any = None
    if "telemetry" in order:
        telemetry = order["telemetry"]
        status_type = (
            telemetry.get("order_status_type") if isinstance(telemetry, dict) else None
        )
        if str(status_type).upper() != "IN_PROGRESS":
            return str(status_type) if status_type is not None else None
    elif "order_status_type" in order:
        status_type = order["order_status_type"]
        if str(status_type).upper() != "IN_PROGRESS":
            return str(status_type) if status_type is not None else None

    status = order.get("status")
    if isinstance(status, dict):
        status = status.get("value") or status.get("text") or status.get("label")
    if status is None:
        status = status_type
    if str(status_type).upper() == "IN_PROGRESS" and status is not None:
        display_status = re.sub(r"[^a-z0-9]+", "_", str(status).strip().lower()).strip(
            "_"
        )
        if _contains_non_negated_status(
            display_status,
            "delivered",
            "completed",
            "finished",
            "cancel",
            "fail",
            "reject",
            "refund",
        ):
            return str(status_type)
    return str(status) if status is not None else None


def normalize_order_status(order: dict[str, Any]) -> str:
    """Map unstable Wolt status text to a fixed Home Assistant enum."""
    raw = _raw_status(order)
    if not raw:
        return "unknown"
    value = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    if _contains_non_negated_status(value, "cancel", "refunded"):
        return "cancelled"
    if _contains_non_negated_status(value, "fail", "reject", "declin"):
        return "failed"
    if _contains_non_negated_status(value, "delivered", "completed", "finished"):
        return "delivered"
    if _contains_non_negated_status(value, "arriv", "nearby", "almost_there"):
        return "arriving"
    if _contains_non_negated_status(
        value, "picked_up", "courier_pickup", "delivery_pickup"
    ):
        return "picked_up"
    if _contains_non_negated_status(value, "ready", "awaiting_pickup"):
        return "ready_for_pickup"
    if _contains_non_negated_status(
        value, "prepar", "production", "restaurant", "acknowledged"
    ):
        return "preparing"
    if _contains_non_negated_status(
        value, "pending", "received", "created", "in_progress", "accepted"
    ):
        return "pending"
    if _contains_non_negated_status(
        value, "on_the_way", "en_route", "courier_delivery", "delivery"
    ):
        return "on_the_way"
    return "unknown"


def _parse_eta(value: Any) -> datetime | None:
    """Parse an ETA without guessing from human-readable duration text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("$date", "date", "value", "timestamp", "max", "end"):
            if key in value and (parsed := _parse_eta(value[key])) is not None:
                return parsed
        return None
    if isinstance(value, int | float):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        # Explicit ETAs must be plausible wall-clock timestamps. Small values
        # are durations/range bounds, not Unix timestamps.
        if not 1_577_836_800 <= timestamp <= 4_102_444_800:
            return None
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except OSError, OverflowError, ValueError:
            return None
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def extract_order_eta(order: dict[str, Any]) -> datetime | None:
    """Extract the first explicit timestamp-shaped ETA."""
    for key in ("delivery_eta", "estimated_delivery_time", "eta"):
        if (parsed := _parse_eta(order.get(key))) is not None:
            return parsed
    estimate = order.get("client_pre_estimate")
    if isinstance(estimate, dict):
        return _parse_eta(estimate.get("delivery_eta"))
    return None
