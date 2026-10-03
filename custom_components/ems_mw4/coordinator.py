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
from . import executor as ex
from .const import (  # noqa: E402
    CONF_GOE_AMP, CONF_GOE_FRC, CONF_GOE_PSM, CONF_SG_INPUT_1, CONF_SG_INPUT_2, MODBUS_BATTERY_SETPOINT,
    MODBUS_HUB, MODBUS_SLAVE, PLAN_MAX_AGE_S, SWITCH_BATTERY, SWITCH_DHW, SWITCH_MASTER, SWITCH_WALLBOX,
    BLOCK_LOG_STORE_KEY, NOTIFY_SERVICE, SWITCH_HEATING, SWITCH_HEAT_BLOCK, SWITCH_QUIET,
    SWITCH_PROACTIVE, SWITCH_CURVE, CONF_COMFORT_TEMP, CONF_HEAT_CURVE, CONF_SUMMER_MODE, COMFORT_WRITE_GAP_S,
)
from . import thermal as th  # noqa: E402
from . import trips as tr  # noqa: E402
from .const import (  # noqa: E402
    CONF_ROUTE_DISTANCE, CONF_ROUTE_DURATION, CONF_TRIP_CALENDAR, ROUTE_MAX_AGE_DAYS, ROUTE_STORE_KEY,
)

ACTION_SNOOZE = "EMS_MW4_SPAETER"
ACTION_SKIP = "EMS_MW4_HEUTE_NICHT"
from datetime import time as dt_time  # noqa: E402

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
        # Ausführer: alles aus, bis ausdrücklich eingeschaltet
        self.switches: dict[str, bool] = {
            SWITCH_MASTER: False, SWITCH_BATTERY: True, SWITCH_DHW: True, SWITCH_WALLBOX: True,
            SWITCH_HEATING: False, SWITCH_HEAT_BLOCK: False, SWITCH_QUIET: False,
            SWITCH_PROACTIVE: False, SWITCH_CURVE: False,
        }
        self._route_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, ROUTE_STORE_KEY)
        self.routes: dict[str, dict[str, Any]] = {}
        self._routes_loaded = False
        self.trip_destination: str | None = None
        self.trips: list[dict[str, Any]] = []
        self.trip_status: str = "noch nicht gelesen"
        self.trip_shapes: list[dict[str, Any]] = []
        self.later_trips: list[dict[str, Any]] = []
        self._last_mileage: float | None = None
        self._arrival: datetime | None = None
        self._reminder_sent_for: datetime | None = None
        self._snooze_until: datetime | None = None
        self._muted_day: Any = None
        self.car_consumption: float = self.params.car_kwh_per_100km
        self.car_consumption_km: float = 0.0
        self._reminded: set[str] = set()
        self.proactive: dict[str, Any] = {"shift_k": 0.0, "grund": "noch nicht gerechnet"}
        self._comfort_written_at: datetime | None = None
        self._comfort_touched = False
        self.quiet_start: dt_time = dt_time(23, 0)
        self.quiet_end: dt_time = dt_time(3, 0)
        self._block_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, BLOCK_LOG_STORE_KEY)
        self.block_log: list[dict[str, Any]] = []
        self._block_open: dict[str, Any] | None = None
        self._trouble_since: datetime | None = None
        self._last_push: datetime | None = None
        self.intent: dict[str, Any] = {"grund": "noch nicht gerechnet"}
        self.last_written: dict[str, Any] = {}
        self._psm_changes: list[datetime] = []

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
        blocks = await self._block_store.async_load()
        if blocks and isinstance(blocks.get("log"), list):
            self.block_log = blocks["log"][-500:]
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen("mobile_app_notification_action", self.handle_notification_action)
        )
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
        sample["heat_shift"] = self.proactive.get("shift_k")
        sample["heat_curve"] = self._number(CONF_HEAT_CURVE)
        sample["heat_comfort"] = self._number(CONF_COMFORT_TEMP)
        i = self.plan_index()
        if i is not None:
            for key, name in (("grid_kw", "plan_grid_kw"), ("battery_kw", "plan_battery_kw"), ("soc", "plan_soc"),
                              ("battery_action", "plan_action"), ("pv_kw", "plan_pv_kw"), ("dhw_kw", "plan_dhw_kw"),
                              ("car_kw", "plan_car_kw"), ("base_kw", "plan_base_kw"), ("heat_kw", "plan_heat_kw"),
                              ("heat_mode", "plan_heat_mode")):
                sample[name] = self.plan[key][i]
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
        await self._block_store.async_save({"log": self.block_log})

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
        try:
            trips = await self._async_trips(now, slots)
        except Exception:  # noqa: BLE001 - Kalender oder Fahrzeit dürfen den Plan nie verhindern
            _LOGGER.exception("Fahrten konnten nicht gelesen werden")
            trips, self.trip_status = [], "Fehler beim Lesen"
        plan = await self.hass.async_add_executor_job(
            build_plan, slots, prices, estimated, pv, base, heat, soc, self.data.get("dhw_temp"),
            self.data.get("car_connected") == 1.0, self.data.get("car_soc"), history, self.params,
            temps, self.data.get(KEY_ROOM_TEMP), self.quiet_slots(slots), trips,
        )
        plan["temp_c"] = temps
        self._update_proactive(temps)
        self.plan, self.plan_time = plan, now
        self.plan_status = "ok" if not any(estimated[:96]) else "ok, Preise teils geschätzt"

    def quiet_slots(self, slots: list[datetime]) -> tuple[int, int] | None:
        """Slot-Bereich des einmaligen Ruhefensters (nächstes Vorkommen), falls eingeschaltet."""
        if not self.switches[SWITCH_QUIET] or self.quiet_start == self.quiet_end:
            return None

        def inside(moment: datetime) -> bool:
            t = moment.time()
            if self.quiet_start < self.quiet_end:
                return self.quiet_start <= t < self.quiet_end
            return t >= self.quiet_start or t < self.quiet_end

        start = next((i for i, s in enumerate(slots) if inside(s)), None)
        if start is None:
            return None
        end = start
        while end < len(slots) and inside(slots[end]):
            end += 1
        return start, end

    def plan_index(self) -> int | None:
        """Index des laufenden Slots im Plan."""
        if not self.plan:
            return None
        current = fc.slot_start(dt_util.now())
        for i, slot in enumerate(self.plan["slots"]):
            if slot == current:
                return i
        return None

    # ---------- Fahrten aus dem Kalender ----------

    async def _async_trips(self, now: datetime, slots: list[datetime]) -> list[dict[str, Any]]:
        """Termine mit Adresse lesen, fehlende Strecken abfragen (höchstens zwei je Lauf), Fahrten berechnen."""
        if not self._routes_loaded:
            self.routes = (await self._route_store.async_load() or {}).get("routes", {})
            self._routes_loaded = True
        if self.hass.states.get(CONF_TRIP_CALENDAR) is None or not self.hass.services.has_service("calendar", "get_events"):
            self.trips, self.trip_status = [], "Kalender nicht verfügbar"
            return []
        events = await self._async_events_direct(now)
        source = "volle Adressen"
        if events is None:
            source = "nur Ortsname"
            response = await self.hass.services.async_call(
                "calendar", "get_events", {"entity_id": CONF_TRIP_CALENDAR, "duration": {"hours": 168}},
                blocking=True, return_response=True,
            )
            events = (response or {}).get(CONF_TRIP_CALENDAR, {}).get("events", [])
        self.trip_shapes = [e["shape"] for e in events if e.get("shape") and e["shape"].get("felder")][:6]
        found = tr.parse_events(events, now, self.params)
        stale = now - timedelta(days=ROUTE_MAX_AGE_DAYS)
        lookups = 0
        for trip in found:
            route = self.routes.get(trip["address"])
            fresh = route is not None and datetime.fromisoformat(route["t"]) > stale
            if not fresh and lookups < 1:  # der Fahrzeit-Dienst nimmt nur eine Abfrage je 10 s an
                lookups += 1
                new = await self._async_route(trip.get("coords"), now)
                if new is not None:
                    self.routes[trip["address"]] = new
                    self._route_store.async_delay_save(lambda: {"routes": self.routes}, 5)
        from dataclasses import replace

        self.car_consumption, self.car_consumption_km = tr.learn_consumption(
            self.samples, self.params.car_kwh, self.params.car_kwh_per_100km
        )
        learned = replace(self.params, car_kwh_per_100km=self.car_consumption)
        self.trips = tr.with_routes(found, self.routes, slots, learned)
        self.later_trips = tr.later_trips(found, self.routes, slots, learned)
        missing = len({t["address"] for t in found if t["address"] not in self.routes})
        self.trip_status = (
            f"{len(found)} Termine mit Adresse" + (f", {missing} ohne Strecke" if missing else "") + f" ({source})"
        )
        return self.trips

    async def _async_events_direct(self, now: datetime) -> list[dict[str, Any]] | None:
        """Termine samt voller Adresse aus der laufenden Kalender-Integration holen. None, wenn das nicht geht.

        Die Kalender-Entität liefert nur den Anzeigenamen des Ortes. Die Terminobjekte der Integration
        enthalten Straße, Postleitzahl und Ort. Eigene Zugangsdaten braucht EMS MW4 dafür nicht.
        """
        state = self.hass.states.get(CONF_TRIP_CALENDAR)
        wanted = (state.attributes.get("friendly_name") or "").strip() if state else ""
        try:
            for entry in self.hass.config_entries.async_entries("ms365_calendar"):
                for cal in getattr(getattr(entry, "runtime_data", None), "coordinator", None) or []:
                    config = getattr(cal, "entity", None)
                    name = str(config.get("name") if isinstance(config, dict) else "").strip()
                    if not wanted or name != wanted:
                        continue
                    return tr.events_from_objects(await cal.async_get_events(now, now + timedelta(days=7)))
        except Exception:  # noqa: BLE001 - fremde Integration: bei jeder Abweichung auf den Standardweg zurückfallen
            _LOGGER.warning("Kalender direkt nicht lesbar, nutze den Standardweg", exc_info=True)
        return None

    async def _async_route(self, address: str | None, now: datetime) -> dict[str, Any] | None:
        """Entfernung und Fahrzeit über die Fahrzeit-Sensoren holen. Ziel ist der Sensor „Fahrtziel"."""
        before = self.hass.states.get(CONF_ROUTE_DISTANCE)
        if before is None or not address:
            return None  # der Fahrzeit-Dienst braucht Koordinaten
        # Zeitstempel sichern: bei gleichem Wert ändert Home Assistant dasselbe Zustandsobjekt
        stamp = before.last_reported
        self.trip_destination = address
        self.async_update_listeners()
        await self.hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": [CONF_ROUTE_DISTANCE, CONF_ROUTE_DURATION]}, blocking=True
        )
        import asyncio

        for _ in range(15):  # der Dienst verzögert Abfragen bis zu 10 s nach der vorigen
            after = self.hass.states.get(CONF_ROUTE_DISTANCE)
            if after is not None and after.last_reported > stamp:
                break
            await asyncio.sleep(1)
        after, duration = self.hass.states.get(CONF_ROUTE_DISTANCE), self.hass.states.get(CONF_ROUTE_DURATION)
        if after is None or duration is None or after.last_reported <= stamp:
            return None  # keine neue Antwort: alten Wert nicht fälschlich dieser Adresse zuordnen
        km, minutes = to_number(after.state), to_number(duration.state)
        if km is None or minutes is None:
            return None
        return {"km": round(km, 1), "min": round(minutes, 1), "t": now.isoformat()}

    def charge_needed_kwh(self) -> float:
        """Was dem Auto für geplante Fahrten oder die Grundreserve fehlt, solange es nicht lädt."""
        need = sum((t.get("fehlt_kwh") or 0.0) for t in (self.plan or {}).get("trips", []))
        soc = (self.data or {}).get("car_soc")
        if soc is not None and soc < self.params.car_reserve_soc:
            need = max(need, (self.params.car_reserve_soc - soc) / 100.0 * self.params.car_kwh)
        return round(need, 1)

    async def _async_reminder(self) -> None:
        """Erinnerung ans Anstecken: frühestens 30 Minuten nach Ankunft, nicht zwischen 22 und 7 Uhr.

        Ankunft = der Kilometerstand hat sich geändert. Während einer geplanten Abwesenheit wird nicht erinnert.
        """
        now = dt_util.now()
        data = self.data or {}
        mileage = data.get("car_mileage")
        if mileage is not None:
            if self._last_mileage is not None and mileage > self._last_mileage:
                self._arrival, self._snooze_until = now, None
            self._last_mileage = mileage
        if self._arrival is None or data.get("car_connected") != 0.0:
            return
        if now.hour >= 22 or now.hour < 7 or self._muted_day == now.date():
            return
        if any(
            datetime.fromisoformat(t["abfahrt"]) <= now <= datetime.fromisoformat(t["rueckkehr"])
            for t in (self.plan or {}).get("trips", [])
        ):
            return
        due = self._snooze_until if self._snooze_until is not None else self._arrival + timedelta(minutes=30)
        if now < due or (self._snooze_until is None and self._reminder_sent_for == self._arrival):
            return
        need = self.charge_needed_kwh()
        if need <= 0:
            return
        self._reminder_sent_for, self._snooze_until = self._arrival, None
        if self.hass.services.has_service("notify", NOTIFY_SERVICE):
            await self.hass.services.async_call("notify", NOTIFY_SERVICE, {
                "title": "EMS MW4: Auto anstecken",
                "message": f"Es fehlen {need} kWh für die nächsten Fahrten. Das Auto ist nicht angesteckt.",
                "data": {"actions": [
                    {"action": ACTION_SNOOZE, "title": "In 1 Stunde erneut"},
                    {"action": ACTION_SKIP, "title": "Heute nicht"},
                ]},
            })

    @callback
    def handle_notification_action(self, event: Any) -> None:
        """Antwort auf die Tasten der Erinnerung."""
        action = event.data.get("action")
        if action == ACTION_SNOOZE:
            self._snooze_until = dt_util.now() + timedelta(hours=1)
        elif action == ACTION_SKIP:
            self._muted_day = dt_util.now().date()

    # ---------- Vorausschauend heizen ----------

    def _number(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        return to_number(state.state) if state is not None else None

    def _update_proactive(self, temps: list[float | None]) -> None:
        """Empfehlung für Komforttemperatur und Heizkurve neu rechnen (alle 15 Minuten mit dem Plan)."""
        now = dt_util.utcnow()
        p = self.params
        adv = th.advise(
            (self.data or {}).get(KEY_ROOM_TEMP), th.room_slope(self.samples, now, 12),
            th.outdoor_past_mean(self.samples, now, 24), temps[: int(p.heat_lookahead_h * 4)], p,
        )
        curve = self._number(CONF_HEAT_CURVE)
        recent = self.samples[-3 * 96 :]
        steady = bool(recent) and all(s.get("heat_curve") == curve for s in recent)
        history = [s["heat_shift"] for s in recent if s.get("heat_shift") is not None]
        summer = self.hass.states.get(CONF_SUMMER_MODE)
        adv.update({
            "comfort_target_c": round(p.heat_comfort_base_c + adv["shift_k"], 1),
            "comfort_now_c": self._number(CONF_COMFORT_TEMP),
            "curve_now": curve,
            "curve_target": th.curve_target(curve, history, p) if steady else None,
            "sommerbetrieb": summer.state == STATE_ON if summer is not None else None,
        })
        self.proactive = adv

    async def _async_proactive(self) -> dict[str, Any]:
        """Komforttemperatur (höchstens alle 3 Stunden) und Heizkurve schreiben."""
        written: dict[str, Any] = {}
        adv, now = self.proactive, dt_util.utcnow()
        call = self.hass.services.async_call
        target, current = adv.get("comfort_target_c"), self._number(CONF_COMFORT_TEMP)
        due = self._comfort_written_at is None or (now - self._comfort_written_at).total_seconds() >= COMFORT_WRITE_GAP_S
        if target is not None and current is not None and adv.get("predicted_c") is not None:
            if abs(target - current) >= 0.05 and due:
                await call("number", "set_value", {"entity_id": CONF_COMFORT_TEMP, "value": target}, blocking=True)
                self._comfort_written_at, self._comfort_touched = now, True
                current = target
            written["comfort_c"] = current
        curve = adv.get("curve_target")
        if self.switches[SWITCH_CURVE] and curve is not None and self._number(CONF_HEAT_CURVE) is not None:
            await call("number", "set_value", {"entity_id": CONF_HEAT_CURVE, "value": curve}, blocking=True)
            written["curve"] = curve
            adv["curve_target"] = None
        return written

    # ---------- Einstellungen ----------

    def set_param(self, key: str, value: float) -> None:
        """Parameter ändern und neu planen."""
        from dataclasses import replace

        self.params = replace(self.params, **{key: value})
        self._model_day = None if key == "heat_limit_c" else self._model_day
        if self.data:
            self.config_entry.async_create_background_task(self.hass, self.async_replan(), "ems_mw4_plan_einstellung")

    # ---------- Ausführer ----------

    async def async_execute(self, _now: datetime | None = None) -> None:
        """Befehle aus dem Plan ableiten. Geschrieben wird nur bei aktiver Steuerung."""
        try:
            await self._async_execute()
        except Exception:  # noqa: BLE001 - nie die Integration mitreißen; Geräte fallen selbst zurück
            _LOGGER.exception("Ausführer fehlgeschlagen")
            self.intent = {"grund": "Fehler im Ausführer"}
        self.async_update_listeners()

    async def _async_execute(self) -> None:
        age = (dt_util.now() - self.plan_time).total_seconds() if self.plan_time else None
        intent = ex.decide(self.plan, self.plan_index(), self.data or {}, age, PLAN_MAX_AGE_S, self.params)
        self.intent = intent
        written: dict[str, Any] = {}
        self._reset_quiet_when_over()
        await self._async_reminder()
        if not self.switches[SWITCH_MASTER]:
            self.last_written = {}
            return
        call = self.hass.services.async_call
        # Speicher: alle 30 s neu schreiben, sonst übernimmt der Kostal nach 60 s
        if self.switches[SWITCH_BATTERY] and intent.get("battery_w") is not None:
            await call(
                "modbus", "write_register",
                {"hub": MODBUS_HUB, "slave": MODBUS_SLAVE, "address": MODBUS_BATTERY_SETPOINT,
                 "value": ex.float_words(intent["battery_w"])},
                blocking=True,
            )
            written["battery_w"] = intent["battery_w"]
        # Wärmepumpe über SG Ready: 1 = Sperre, 2 = normal, 3 = anheben. Nur bei Änderung schreiben.
        target = ex.sg_state(intent.get("dhw"), intent.get("heat"), self.switches[SWITCH_DHW], self.switches[SWITCH_HEATING])
        if target is not None:
            for entity_id, want in zip((CONF_SG_INPUT_2, CONF_SG_INPUT_1), reversed(ex.SG_INPUTS[target])):
                state = self.hass.states.get(entity_id)
                if state is not None and state.state in ("on", "off") and state.state != want:
                    await call("switch", f"turn_{want}", {"entity_id": entity_id}, blocking=True)
            written["sg_ready"] = target
            if self.switches[SWITCH_DHW] and intent.get("dhw") is not None:
                written["dhw"] = intent["dhw"]
        if self.switches[SWITCH_HEATING] and self.switches[SWITCH_PROACTIVE]:
            written.update(await self._async_proactive())
        elif self._comfort_touched and self._number(CONF_COMFORT_TEMP) is not None:
            await call(
                "number", "set_value",
                {"entity_id": CONF_COMFORT_TEMP, "value": self.params.heat_comfort_base_c}, blocking=True,
            )
            self._comfort_touched, self._comfort_written_at = False, None
        self._track_block(target, intent)
        await self._async_watch()
        # Wallbox
        if self.switches[SWITCH_WALLBOX] and intent.get("car") is not None:
            written["car"] = await self._async_wallbox(intent["car"])
        self.last_written = written

    async def _async_wallbox(self, want: dict[str, Any]) -> dict[str, Any]:
        """Wallbox setzen. Phasen nur ohne Last, höchstens alle 10 Minuten und 6-mal am Tag."""
        call = self.hass.services.async_call
        done: dict[str, Any] = {}

        def current(entity_id: str) -> str | None:
            state = self.hass.states.get(entity_id)
            return None if state is None or state.state in ("unavailable", "unknown") else state.state

        frc, psm, amp = current(CONF_GOE_FRC), current(CONF_GOE_PSM), current(CONF_GOE_AMP)
        if frc is None:
            return {"grund": "Wallbox nicht erreichbar"}
        if want["frc"] == "dont_charge":
            if frc != "dont_charge":
                await call("select", "select_option", {"entity_id": CONF_GOE_FRC, "option": "dont_charge"}, blocking=True)
                done["frc"] = "dont_charge"
            return done
        if psm is not None and psm != want["psm"]:
            now = dt_util.utcnow()
            self._psm_changes = [t for t in self._psm_changes if (now - t).total_seconds() < 86400]
            too_soon = self._psm_changes and (now - self._psm_changes[-1]).total_seconds() < 600
            if too_soon or len(self._psm_changes) >= 6:
                want = {**want, "psm": psm}  # Phasen bleiben, Strom passend zur vorhandenen Phasenzahl
                kw = self.plan["car_kw"][self.plan_index()]
                phases = 1 if psm == "one_phase" else 3
                want["amp"] = max(6, min(16, round(kw * 1000 / (phases * 230))))
            elif (self.data.get("wallbox_power") or 0) > 50:
                if frc != "dont_charge":
                    await call("select", "select_option", {"entity_id": CONF_GOE_FRC, "option": "dont_charge"}, blocking=True)
                return {"frc": "dont_charge", "grund": "Phasenwechsel: warte auf Strom 0"}
            else:
                await call("select", "select_option", {"entity_id": CONF_GOE_PSM, "option": want["psm"]}, blocking=True)
                self._psm_changes.append(now)
                return {"psm": want["psm"], "grund": "Phasen umgeschaltet, Laden im nächsten Schritt"}
        if amp is not None and int(float(amp)) != want["amp"]:
            await call("number", "set_value", {"entity_id": CONF_GOE_AMP, "value": want["amp"]}, blocking=True)
            done["amp"] = want["amp"]
        if frc != "charge":
            await call("select", "select_option", {"entity_id": CONF_GOE_FRC, "option": "charge"}, blocking=True)
            done["frc"] = "charge"
        return done

    async def async_release(self) -> None:
        """Steuerung abgeben: Speicher fällt nach 60 s von selbst zurück, SG Ready normal, Wallbox neutral."""
        call = self.hass.services.async_call
        for entity_id in (CONF_SG_INPUT_1, CONF_SG_INPUT_2):
            state = self.hass.states.get(entity_id)
            if state is not None and state.state == "on":
                await call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
        state = self.hass.states.get(CONF_GOE_FRC)
        if state is not None and state.state in ("charge", "dont_charge"):
            await call("select", "select_option", {"entity_id": CONF_GOE_FRC, "option": "neutral"}, blocking=True)
        if self._comfort_touched and self._number(CONF_COMFORT_TEMP) is not None:
            await call(
                "number", "set_value",
                {"entity_id": CONF_COMFORT_TEMP, "value": self.params.heat_comfort_base_c}, blocking=True,
            )
            self._comfort_touched = False
        self.last_written = {}

    # ---------- Heizung: Ruhefenster, Sperrprotokoll, Wächter ----------

    def _reset_quiet_when_over(self) -> None:
        """Ruhefenster gilt eine Nacht: nach dem Ende schaltet es sich aus."""
        if not self.switches[SWITCH_QUIET]:
            self._quiet_seen = False
            return
        now = dt_util.now().time()
        if self.quiet_start < self.quiet_end:
            inside = self.quiet_start <= now < self.quiet_end
        else:
            inside = now >= self.quiet_start or now < self.quiet_end
        if inside:
            self._quiet_seen = True
        elif getattr(self, "_quiet_seen", False):
            self.switches[SWITCH_QUIET] = False
            self._quiet_seen = False
            self.config_entry.async_create_background_task(self.hass, self.async_replan(), "ems_mw4_ruhe_ende")

    def _track_block(self, target: int | None, intent: dict[str, Any]) -> None:
        """Jede ausgeführte Sperre protokollieren: Dauer, Raumtemperatur, Außentemperatur."""
        now = dt_util.now()
        room = (self.data or {}).get(KEY_ROOM_TEMP)
        blocked = target == 1
        if blocked and self._block_open is None:
            self._block_open = {
                "start": now.isoformat(timespec="minutes"), "art": intent.get("heat"), "raum_start": room,
                "raum_min": room, "aussen": (self.data or {}).get("outdoor_temp"),
                "verdichter_starts": 0, "_last_comp": (self.data or {}).get("compressor"),
            }
        elif self._block_open is not None:
            entry = self._block_open
            if room is not None and (entry["raum_min"] is None or room < entry["raum_min"]):
                entry["raum_min"] = room
            comp = (self.data or {}).get("compressor")
            if comp == 1.0 and entry["_last_comp"] == 0.0:
                entry["verdichter_starts"] += 1
            entry["_last_comp"] = comp
            if not blocked:
                entry.pop("_last_comp", None)
                entry["ende"] = now.isoformat(timespec="minutes")
                entry["raum_ende"] = room
                entry["abbruch"] = intent.get("heat_grund")
                self.block_log.append(entry)
                del self.block_log[:-500]
                self._block_open = None
                self._block_store.async_delay_save(lambda: {"log": self.block_log}, 30)

    async def _async_watch(self) -> None:
        """Bei aktiver Steuerung: Push, wenn Messwerte oder Plan länger als 10 Minuten fehlen."""
        now = dt_util.utcnow()
        trouble = bool(self.missing) or not self.plan_status.startswith("ok")
        if not trouble:
            self._trouble_since = None
            return
        if self._trouble_since is None:
            self._trouble_since = now
            return
        if (now - self._trouble_since).total_seconds() < 600:
            return
        if self._last_push is not None and (now - self._last_push).total_seconds() < 3600:
            return
        if not self.hass.services.has_service("notify", NOTIFY_SERVICE):
            return
        self._last_push = now
        text = f"Plan: {self.plan_status}. Fehlende Messwerte: {', '.join(self.missing) or 'keine'}."
        await self.hass.services.async_call(
            "notify", NOTIFY_SERVICE, {"title": "EMS MW4: Störung", "message": text}, blocking=False
        )
