"""Rohdaten-Sensoren von EMS MW4."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EmsConfigEntry
from .const import DOMAIN, KEY_ROOM_TEMP, KEY_SAMPLES, KEY_SOURCES_OK, NAME, SOURCES, Source
from .coordinator import EmsCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: EmsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [EmsSourceSensor(coordinator, entry, s) for s in SOURCES]
    entities.append(EmsRoomSensor(coordinator, entry))
    entities.append(EmsStatusSensor(coordinator, entry))
    entities.append(EmsSamplesSensor(coordinator, entry))
    async_add_entities(entities)


class EmsBaseSensor(CoordinatorEntity[EmsCoordinator], SensorEntity):
    """Gemeinsame Basis."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="Eigenbau",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        return self.coordinator.data.get(self._key)


class EmsSourceSensor(EmsBaseSensor):
    """Spiegelt eine Quelle, bereinigt und ggf. entprellt."""

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, source: Source) -> None:
        super().__init__(coordinator, entry, source.key, source.name)
        self._source = source
        self._attr_native_unit_of_measurement = source.unit
        self._attr_device_class = source.device_class
        self._attr_suggested_display_precision = source.precision
        if source.unit is not None and source.device_class not in ("energy", "distance"):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "quelle": self.coordinator.entity_for(self._key),
            "entprellt": self._source.debounce,
        }


class EmsRoomSensor(EmsBaseSensor):
    """Raumreferenz: Median der gewählten Raumfühler."""

    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = "temperature"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, KEY_ROOM_TEMP, "Raumtemperatur Referenz")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "fuehler": self.coordinator.room_sensors,
            "fuehler_gueltig": self.coordinator.data.get("room_sensors_used"),
        }


class EmsStatusSensor(EmsBaseSensor):
    """Anzahl der Quellen mit gültigem Wert."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, KEY_SOURCES_OK, "Datenquellen gültig")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"gesamt": len(SOURCES) + 1, "fehlend": list(self.coordinator.missing)}


class EmsSamplesSensor(EmsBaseSensor):
    """Anzahl gespeicherter Messpunkte."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, KEY_SAMPLES, "Gespeicherte Messpunkte")
