"""Einstellungen von EMS MW4 als Zahlen."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EmsConfigEntry
from .const import DOMAIN, NAME, SETTINGS, Setting
from .coordinator import EmsCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: EmsConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(EmsSettingNumber(entry.runtime_data, entry, s) for s in SETTINGS)


class EmsSettingNumber(RestoreNumber):
    """Ein Planer-Parameter. Der Wert übersteht Neustarts."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, setting: Setting) -> None:
        self._coordinator = coordinator
        self._setting = setting
        self._attr_name = setting.name
        self._attr_unique_id = f"{entry.entry_id}_set_{setting.key}"
        self._attr_native_min_value = setting.minimum
        self._attr_native_max_value = setting.maximum
        self._attr_native_step = setting.step
        self._attr_native_unit_of_measurement = setting.unit
        self._attr_icon = setting.icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=NAME, manufacturer="Eigenbau", entry_type=DeviceEntryType.SERVICE
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            value = min(self._setting.maximum, max(self._setting.minimum, float(last.native_value)))
            if value != getattr(self._coordinator.params, self._setting.key):
                self._coordinator.set_param(self._setting.key, value)

    @property
    def native_value(self) -> float:
        return getattr(self._coordinator.params, self._setting.key)

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_param(self._setting.key, float(value))
        self.async_write_ha_state()
