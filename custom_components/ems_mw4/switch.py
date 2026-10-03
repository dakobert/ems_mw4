"""Schalter von EMS MW4: Steuerung gesamt und Automatik je Gerät."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EmsConfigEntry
from .const import (
    DOMAIN, NAME, SWITCH_BATTERY, SWITCH_DHW, SWITCH_HEAT_BLOCK, SWITCH_HEATING, SWITCH_MASTER, SWITCH_QUIET,
    SWITCH_PROACTIVE,
    SWITCH_CURVE,
    SWITCH_CAR_NOW,
    SWITCH_WALLBOX,
)
from .coordinator import EmsCoordinator

SWITCHES = (
    (SWITCH_MASTER, "Steuerung aktiv", "mdi:power"),
    (SWITCH_BATTERY, "Automatik Speicher", "mdi:home-battery"),
    (SWITCH_DHW, "Automatik Warmwasser", "mdi:water-boiler"),
    (SWITCH_WALLBOX, "Automatik Wallbox", "mdi:ev-station"),
    (SWITCH_HEATING, "Automatik Heizung", "mdi:heat-pump"),
    (SWITCH_HEAT_BLOCK, "Sperre in Preisspitzen", "mdi:cash-lock"),
    (SWITCH_QUIET, "Ruhefenster einmalig", "mdi:sleep"),
    (SWITCH_PROACTIVE, "Vorausschauend heizen", "mdi:crystal-ball"),
    (SWITCH_CURVE, "Heizkurve automatisch anpassen", "mdi:chart-bell-curve-cumulative"),
    (SWITCH_CAR_NOW, "Auto jetzt voll laden", "mdi:car-electric"),
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
            self._apply()

    def _apply(self) -> None:
        """Schalter, die den Plan ändern."""
        on = self._coordinator.switches[self._key]
        if self._key == SWITCH_HEAT_BLOCK:
            if self._coordinator.params.heat_block_enabled != on:
                self._coordinator.set_param("heat_block_enabled", on)
        elif self._key == SWITCH_CAR_NOW:
            if self._coordinator.params.car_now != on:
                self._coordinator.set_param("car_now", on)
        elif self._key == SWITCH_QUIET and self._coordinator.data:
            self._coordinator.config_entry.async_create_background_task(
                self.hass, self._coordinator.async_replan(), "ems_mw4_plan_ruhefenster"
            )

    @property
    def should_poll(self) -> bool:
        return self._key == SWITCH_QUIET  # setzt sich selbst zurück

    @property
    def is_on(self) -> bool:
        return self._coordinator.switches[self._key]

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._coordinator.switches[self._key] = True
        self._apply()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._coordinator.switches[self._key] = False
        self._apply()
        self.async_write_ha_state()
        if self._key == SWITCH_MASTER:
            await self._coordinator.async_release()
