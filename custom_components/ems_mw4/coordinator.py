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

from . import data as ems_data
from . import forecast as fc
from .const import (  # noqa: I001
    CONF_WEATHER,
    COST_STORE_KEY,
    DEFAULT_WEATHER,
    HORIZON_SLOTS,
    PV_FORECAST_KEYS,
    Params,
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

from .planner import build_plan

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
        # Phase 2
        self.params = Params()
        self._cost_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, COST_STORE_KEY)
        self.costs: dict[str, dict[str, float]] = {}
        self._last_cost_time: datetime | None = None
        self.plan: dict[str, Any] | None = None
        self.plan_time: datetime | None = None
        self.plan_status: str = "noch kein Plan"
        self.base_profile: dict[tuple[bool, int], float] = {}
        self.heat_w_per_k: float = self.params.heat_w_per_k
        self.heat_fit_days: int = 0
        self._model_day: Any = None
        self._replanning = False

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
        costs = await self._cost_store.async_load()
        if costs and isinstance(costs.get("days"), dict):
            self.costs = costs["days"]

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
        self._track_cost(now, data.get("grid_power"), data.get("price"))
        data[KEY_SOURCES_OK] = len(SOURCES) + 1 - len(missing)
        data[KEY_SAMPLES] = len(self.samples)
        if self.plan is None and not self._replanning and data.get("battery_soc") is not None and self.data:
            # nach dem Start: Plan nachholen, sobald der Speicher-Ladestand vorliegt
            self.config_entry.async_create_background_task(self.hass, self.async_replan(), "ems_mw4_plan_nachholen")
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
        """Messreihe und Kosten sofort schreiben (beim Entladen)."""
        await self._store.async_save(self._data_to_save())
        await self._cost_store.async_save(self._costs_to_save())

    # ---------- Kosten ----------

    @callback
    def _costs_to_save(self) -> dict[str, Any]:
        return {"days": self.costs}

    def _track_cost(self, now: datetime, grid_w: float | None, price_ct: float | None) -> None:
        """Netzbezug mit dem aktuellen Preis aufsummieren. Fehlende Werte zählen nicht."""
        last, self._last_cost_time = self._last_cost_time, now
        if last is None or grid_w is None or price_ct is None:
            return
        hours = (now - last).total_seconds() / 3600.0
        if hours <= 0 or hours > 5 / 60:
            return  # Lücke (Neustart): nicht schätzen
        kwh = max(0.0, grid_w) / 1000.0 * hours
        day = self.costs.setdefault(dt_util.as_local(now).date().isoformat(), {"kwh": 0.0, "eur": 0.0})
        day["kwh"] = round(day["kwh"] + kwh, 5)
        day["eur"] = round(day["eur"] + kwh * price_ct / 100.0, 5)
        if len(self.costs) > 800:
            for key in sorted(self.costs)[: len(self.costs) - 800]:
                del self.costs[key]
        self._cost_store.async_delay_save(self._costs_to_save, 300)

    def cost_sum(self, days: list[str], field: str = "eur") -> float:
        return round(sum(self.costs.get(d, {}).get(field, 0.0) for d in days), 2)

    # ---------- Prognose und Plan ----------

    async def _async_update_models(self) -> None:
        """Grundlastprofil und Heizkennwert einmal täglich aus der Statistik."""
        today = dt_util.now().date()
        if self._model_day == today:
            return
        home, hp, wb, out = (self.entity_for(k) for k in ("home_power", "hp_power", "wallbox_power", "outdoor_temp"))
        hp_factor = next(s.factor for s in SOURCES if s.key == "hp_power")
        recent = await ems_data.async_fetch_hourly_means(self.hass, [home, hp, wb], 28)
        if recent.get(home):
            hp_w = {k: v * hp_factor for k, v in recent.get(hp, {}).items()}
            self.base_profile = fc.base_load_profile(ems_data.base_rows(recent[home], hp_w, recent.get(wb, {})))
        long = await ems_data.async_fetch_hourly_means(self.hass, [hp, out], 400)
        if long.get(hp) and long.get(out):
            hp_w = {k: v * hp_factor for k, v in long[hp].items()}
            self.heat_w_per_k, self.heat_fit_days = fc.fit_heat(
                ems_data.heat_days(long[out], hp_w), self.params.heat_limit_c, self.params.heat_w_per_k
            )
        self._model_day = today

    async def async_replan(self, _now: datetime | None = None) -> None:
        """Plan neu rechnen. Fehlen Preise oder Ladestand, bleibt der letzte Plan stehen."""
        if self._replanning:
            return
        self._replanning = True
        try:
            await self._async_replan()
        except Exception:  # noqa: BLE001 - der Planer darf die Integration nie mitreißen
            _LOGGER.exception("Planrechnung fehlgeschlagen")
            self.plan_status = "Fehler in der Planrechnung"
        finally:
            self._replanning = False
        self.async_update_listeners()

    async def _async_replan(self) -> None:
        if not self.data:
            return
        now = dt_util.now()
        slots = fc.build_slots(now, HORIZON_SLOTS)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        known = await ems_data.async_fetch_prices(self.hass, midnight - timedelta(days=1), 4)
        prices, estimated = fc.price_series(slots, known)
        if any(p is None for p in prices):
            self.plan_status = "keine Preise, letzter Plan gilt weiter"
            return
        soc = self.data.get("battery_soc")
        if soc is None:
            self.plan_status = "Speicher-Ladestand fehlt, letzter Plan gilt weiter"
            return
        await self._async_update_models()
        weather = self.config_entry.options.get(CONF_WEATHER, DEFAULT_WEATHER)
        temps = fc.temp_series(
            slots, await ems_data.async_fetch_temperatures(self.hass, weather), self.data.get("outdoor_temp")
        )
        pv = fc.pv_series(
            slots, ems_data.pv_hours(self.hass, [self.entity_for(k) for k in PV_FORECAST_KEYS], now.date())
        )
        base = fc.base_load_series(slots, self.base_profile, self.params.base_load_default_w)
        heat = fc.heat_series(temps, self.params.heat_limit_c, self.heat_w_per_k)
        history = [s.get("price") for s in self.samples[-14 * 96 :] if s.get("price") is not None]
        if len(history) < 2 * 96:
            history = list(known.values())
        plan = await self.hass.async_add_executor_job(
            build_plan, slots, prices, estimated, pv, base, heat, soc, self.data.get("dhw_temp"),
            self.data.get("car_connected") == 1.0, self.data.get("car_soc"), history, self.params,
        )
        plan["temp_c"] = temps
        self.plan, self.plan_time = plan, now
        self.plan_status = "ok" if not any(estimated[:96]) else "ok, Preise teils geschätzt"

    def plan_index(self) -> int | None:
        """Index des laufenden Slots im Plan."""
        if not self.plan:
            return None
        current = fc.slot_start(dt_util.now())
        for i, slot in enumerate(self.plan["slots"]):
            if slot == current:
                return i
        return None
