"""Schalter von EMS MW4: Steuerung gesamt und Automatik je Gerät."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EmsConfigEntry
from .const import DOMAIN, NAME, SWITCH_BATTERY, SWITCH_DHW, SWITCH_MASTER, SWITCH_WALLBOX
from .coordinator import EmsCoordinator

SWITCHES = (
    (SWITCH_MASTER, "Steuerung aktiv", "mdi:power"),
    (SWITCH_BATTERY, "Automatik Speicher", "mdi:home-battery"),
    (SWITCH_DHW, "Automatik Warmwasser", "mdi:water-boiler"),
    (SWITCH_WALLBOX, "Automatik Wallbox", "mdi:ev-station"),
)


async def async_setup_entry(hass: HomeAssistant, entry: EmsConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(EmsSwitch(entry.runtime_data, entry, *spec) for spec in SWITCHES)


class EmsSwitch(SwitchEntity, RestoreEntity):
    """Schalter. 'Steuerung aktiv' ist nach der Einrichtung aus."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, key: str, name: str, icon: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_sw_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=NAME, manufacturer="Eigenbau", entry_type=DeviceEntryType.SERVICE
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._coordinator.switches[self._key] = last.state == "on"

    @property
    def is_on(self) -> bool:
        return self._coordinator.switches[self._key]

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._coordinator.switches[self._key] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._coordinator.switches[self._key] = False
        self.async_write_ha_state()
        if self._key == SWITCH_MASTER:
            await self._coordinator.async_release()
