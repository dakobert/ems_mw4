"""EMS MW4 – Energiemanagement. Phase 1: Datensammler und Rohdaten, keine Steuerung."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .const import SAMPLE_MINUTES
from .coordinator import EmsCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type EmsConfigEntry = ConfigEntry[EmsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EmsConfigEntry) -> bool:
    """Eintrag einrichten."""
    coordinator = EmsCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    entry.async_on_unload(
        async_track_time_change(
            hass, coordinator.async_take_sample, minute=list(SAMPLE_MINUTES), second=5
        )
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: EmsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EmsConfigEntry) -> bool:
    """Eintrag entladen, Messreihe sichern."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_flush()
    return unloaded
