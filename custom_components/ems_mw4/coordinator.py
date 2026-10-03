"""Datensammler: liest die Quellen, entprellt, bildet die Raumreferenz, speichert die Messreihe."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from statistics import median
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ROOM_SENSORS,
    DEFAULT_ROOM_SENSORS,
    DOMAIN,
    KEY_ROOM_TEMP,
    KEY_SAMPLES,
    KEY_SOURCES_OK,
    MAX_SAMPLES,
    ROOM_MAX_AGE_S,
    SOURCES,
    STORE_KEY,
    STORE_VERSION,
    UPDATE_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)

_INVALID = (STATE_UNAVAILABLE, STATE_UNKNOWN, "", "None", "none")


def to_number(state: str | None) -> float | None:
    """Zustand in Zahl wandeln. Ungültig, nicht verfügbar, unbekannt ergibt None."""
    if state is None or state in _INVALID:
        return None
    if state == STATE_ON:
        return 1.0
    if state == STATE_OFF:
        return 0.0
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


class EmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Sammelt Rohdaten. Steuert nichts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_S),
        )
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.samples: list[dict[str, Any]] = []
        self._last_raw: dict[str, float | None] = {}
        self._accepted: dict[str, float | None] = {}
        self.missing: list[str] = []

    def entity_for(self, key: str) -> str:
        """Quell-Entität für einen Schlüssel: Einstellung oder Vorgabe."""
        for source in SOURCES:
            if source.key == key:
                return self.config_entry.options.get(key, source.default)
        raise KeyError(key)

    @property
    def room_sensors(self) -> list[str]:
        return list(self.config_entry.options.get(CONF_ROOM_SENSORS, DEFAULT_ROOM_SENSORS))

    async def async_load(self) -> None:
        """Gespeicherte Messreihe laden."""
        data = await self._store.async_load()
        if data and isinstance(data.get("samples"), list):
            self.samples = data["samples"][-MAX_SAMPLES:]

    def _debounced(self, key: str, raw: float | None) -> float | None:
        """Wert gilt erst, wenn er in zwei Abfragen nacheinander gleich ist."""
        previous = self._last_raw.get(key)
        seen_before = key in self._last_raw
        self._last_raw[key] = raw
        if raw is None:
            self._accepted[key] = None
            return None
        if not seen_before or self._accepted.get(key) is None or raw == previous:
            self._accepted[key] = raw
        return self._accepted[key]

    def _room_temperature(self, now: datetime) -> tuple[float | None, int]:
        values: list[float] = []
        for entity_id in self.room_sensors:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            value = to_number(state.state)
            if value is None:
                continue
            if (now - state.last_reported).total_seconds() > ROOM_MAX_AGE_S:
                continue
            values.append(value)
        if not values:
            return None, 0
        return round(median(values), 2), len(values)

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        data: dict[str, Any] = {}
        missing: list[str] = []
        for source in SOURCES:
            state = self.hass.states.get(self.entity_for(source.key))
            raw = to_number(state.state) if state is not None else None
            if raw is not None:
                raw = raw * source.factor
            value = self._debounced(source.key, raw) if source.debounce else raw
            data[source.key] = value
            if value is None:
                missing.append(source.key)
        room, used = self._room_temperature(now)
        data[KEY_ROOM_TEMP] = room
        data["room_sensors_used"] = used
        if room is None:
            missing.append(KEY_ROOM_TEMP)
        self.missing = missing
        data[KEY_SOURCES_OK] = len(SOURCES) + 1 - len(missing)
        data[KEY_SAMPLES] = len(self.samples)
        return data

    @callback
    def async_take_sample(self, now: datetime) -> None:
        """Messpunkt im 15-Minuten-Raster ablegen."""
        if not self.data:
            return
        sample: dict[str, Any] = {"t": dt_util.as_utc(now).replace(microsecond=0).isoformat()}
        for source in SOURCES:
            sample[source.key] = self.data.get(source.key)
        sample[KEY_ROOM_TEMP] = self.data.get(KEY_ROOM_TEMP)
        self.samples.append(sample)
        if len(self.samples) > MAX_SAMPLES:
            del self.samples[: len(self.samples) - MAX_SAMPLES]
        self._store.async_delay_save(self._data_to_save, 30)
        self.data[KEY_SAMPLES] = len(self.samples)
        self.async_update_listeners()

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {"samples": self.samples}

    async def async_flush(self) -> None:
        """Messreihe sofort schreiben (beim Entladen)."""
        await self._store.async_save(self._data_to_save())
