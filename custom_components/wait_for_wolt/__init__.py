"""Wolt order tracker integration."""

from __future__ import annotations

import uuid

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import WoltApi
from .const import (
    CONF_BEARER_TOKEN,
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    DOMAIN,
)
from .coordinator import WoltDataUpdateCoordinator, WoltRuntimeData

PLATFORMS = [Platform.SENSOR, Platform.GEO_LOCATION]
CONFIG_SCHEMA = cv.platform_only_config_schema(DOMAIN)


def _entry_snapshot(entry: ConfigEntry) -> dict[str, dict]:
    """Return the entry values used by the currently loaded runtime."""
    return {"data": dict(entry.data), "options": dict(entry.options)}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up via YAML (handled by the sensor platform)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the shared client/coordinator and set up entry platforms."""
    from .privacy import migrate_location_consent

    migrate_location_consent(hass, entry)
    remove_purchase_entities(hass, entry)
    if not entry.data.get(CONF_CLIENT_ID):
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CLIENT_ID: str(uuid.uuid4())},
        )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _entry_snapshot(entry)

    def persist_tokens(access_token: str, refresh_token: str) -> None:
        """Persist Wolt credential rotation without reloading the runtime."""
        updated_data = {
            **entry.data,
            CONF_BEARER_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
        }
        runtime_snapshot = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(runtime_snapshot, dict):
            runtime_snapshot["data"] = dict(updated_data)
            runtime_snapshot["options"] = dict(entry.options)
        hass.config_entries.async_update_entry(entry, data=updated_data)

    api = WoltApi(
        async_get_clientsession(hass),
        entry.data.get(CONF_SESSION_ID, ""),
        entry.data[CONF_BEARER_TOKEN],
        entry.data[CONF_REFRESH_TOKEN],
        client_id=entry.data[CONF_CLIENT_ID],
        token_update_callback=persist_tokens,
    )
    coordinator = WoltDataUpdateCoordinator(hass, entry, api)
    entry.runtime_data = WoltRuntimeData(api, coordinator)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    return True


def remove_purchase_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the per-purchase devices and entities of earlier versions.

    Only the account sensors and configured venue sensors are kept; map points
    have no registry entries at all.
    """
    from .sensor import ACCOUNT_SENSOR_KEYS, account_unique_id

    keep = {account_unique_id(entry.entry_id, key) for key in ACCOUNT_SENSOR_KEYS}
    venue_prefix = f"{entry.entry_id}_venue_"
    entities = er.async_get(hass)
    for item in er.async_entries_for_config_entry(entities, entry.entry_id):
        uid = item.unique_id
        if uid in keep or uid.startswith(venue_prefix) or uid.startswith("wolt_venue_"):
            continue
        entities.async_remove(item.entity_id)
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if (DOMAIN, entry.entry_id) not in device.identifiers:
            devices.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let the user delete any device except the account itself."""
    del hass
    return (DOMAIN, entry.entry_id) not in device.identifiers


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after user options change, but not after token rotation."""
    if hass.data.get(DOMAIN, {}).get(entry.entry_id) == _entry_snapshot(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
