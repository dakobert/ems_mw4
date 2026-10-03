"""Beginn und Ende des einmaligen Ruhefensters."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import EmsConfigEntry
from .const import DOMAIN, NAME
from .coordinator import EmsCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: EmsConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([
        EmsQuietTime(c, entry, "quiet_start", "Ruhefenster Beginn"),
        EmsQuietTime(c, entry, "quiet_end", "Ruhefenster Ende"),
    ])


class EmsQuietTime(TimeEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, attr: str, name: str) -> None:
        self._coordinator = coordinator
        self._attr = attr
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_time_{attr}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=NAME, manufacturer="Eigenbau", entry_type=DeviceEntryType.SERVICE
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            parsed = dt_util.parse_time(last.state)
            if parsed is not None:
                setattr(self._coordinator, self._attr, parsed)

    @property
    def native_value(self) -> time:
        return getattr(self._coordinator, self._attr)

    async def async_set_value(self, value: time) -> None:
        setattr(self._coordinator, self._attr, value)
        self.async_write_ha_state()
        if self._coordinator.data:
            self._coordinator.config_entry.async_create_background_task(
                self.hass, self._coordinator.async_replan(), "ems_mw4_plan_ruhezeit"
            )
