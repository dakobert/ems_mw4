"""Rohdaten-Sensoren von EMS MW4."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
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
    entities.extend(plan_entities(coordinator, entry))
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


# ---------- Phase 2: Plan und Kosten ----------

from datetime import datetime, timedelta  # noqa: E402

from homeassistant.util import dt as dt_util  # noqa: E402

from .const import SLOT_H  # noqa: E402
from .planner import next_change, next_start, reason  # noqa: E402


def plan_entities(coordinator: EmsCoordinator, entry: EmsConfigEntry) -> list[SensorEntity]:
    return [
        EmsPlanSensor(coordinator, entry),
        EmsBatteryPlanSensor(coordinator, entry),
        EmsNextStartSensor(coordinator, entry, "plan_dhw", "Plan Warmwasser Start", "dhw_kw"),
        EmsNextStartSensor(coordinator, entry, "plan_car", "Plan Auto Start", "car_kw"),
        EmsCostSensor(coordinator, entry, "cost_today", "Netzbezug Kosten heute", "today"),
        EmsCostSensor(coordinator, entry, "cost_week", "Netzbezug Kosten Woche", "week"),
        EmsCostSensor(coordinator, entry, "cost_month", "Netzbezug Kosten Monat", "month"),
        EmsCostForecastSensor(coordinator, entry, "cost_fc_today", "Netzbezug Kosten Prognose heute", 0),
        EmsCostForecastSensor(coordinator, entry, "cost_fc_tomorrow", "Netzbezug Kosten Prognose morgen", 1),
        EmsImportSensor(coordinator, entry),
        EmsHeatModelSensor(coordinator, entry),
        EmsExecutorSensor(coordinator, entry),
        EmsHeatPlanSensor(coordinator, entry),
        EmsProactiveSensor(coordinator, entry),
        EmsTripDestinationSensor(coordinator, entry),
        EmsTripSensor(coordinator, entry),
    ]


class EmsPlanBase(EmsBaseSensor):
    """Basis für Sensoren, die aus dem Plan lesen statt aus den Rohdaten."""

    @property
    def native_value(self) -> Any:  # pragma: no cover - von Unterklassen überschrieben
        return None


class EmsPlanSensor(EmsPlanBase):
    """Planstatus. Die Zeitreihen stehen in den Attributen (nicht aufgezeichnet)."""

    _unrecorded_attributes = frozenset(
        {"zeit", "preis_ct", "preis_geschaetzt", "pv_kw", "grundlast_kw", "heizung_kw", "warmwasser_kw",
         "auto_kw", "speicher_kw", "speicher_soc", "netz_kw", "speicher_aktion", "aussentemperatur",
         "heizung_modus"}
    )

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "plan", "Plan")

    @property
    def native_value(self) -> str:
        return self.coordinator.plan_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan, when = self.coordinator.plan, self.coordinator.plan_time
        if not plan:
            return {"berechnet": None}
        return {
            "berechnet": when.isoformat(timespec="seconds") if when else None,
            "zeit": [s.isoformat(timespec="minutes") for s in plan["slots"]],
            "preis_ct": [round(v, 2) for v in plan["price_ct"]],
            "preis_geschaetzt": plan["price_estimated"],
            "pv_kw": [round(v, 2) for v in plan["pv_kw"]],
            "grundlast_kw": [round(v, 2) for v in plan["base_kw"]],
            "heizung_kw": [round(v, 2) for v in plan["heat_kw"]],
            "warmwasser_kw": plan["dhw_kw"],
            "auto_kw": [round(v, 2) for v in plan["car_kw"]],
            "speicher_kw": [round(v, 2) for v in plan["battery_kw"]],
            "speicher_soc": plan["soc"],
            "netz_kw": [round(v, 2) for v in plan["grid_kw"]],
            "speicher_aktion": plan["battery_action"],
            "heizung_modus": plan.get("heat_mode"),
            "sperren": [{k: v for k, v in b.items() if not k.endswith("_index")} for b in plan.get("heat_blocks", [])],
            "aussentemperatur": plan.get("temp_c"),
            "kosten_eur": plan["cost_eur"],
            "bezug_kwh": plan["import_kwh"],
            "auto": plan["car"],
        }


class EmsBatteryPlanSensor(EmsPlanBase):
    """Geplante Speicheraktion im laufenden Slot, mit Begründung."""

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "plan_battery", "Plan Speicher")

    @property
    def native_value(self) -> str | None:
        i = self.coordinator.plan_index()
        return None if i is None else self.coordinator.plan["battery_action"][i]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        i = self.coordinator.plan_index()
        if i is None:
            return {}
        plan = self.coordinator.plan
        j = next_change(plan["battery_action"], i)
        return {
            "begruendung": reason(plan, i),
            "leistung_kw": plan["battery_kw"][i],
            "ladestand_plan": plan["soc"][i],
            "bis": plan["slots"][j].isoformat(timespec="minutes") if j is not None else None,
            "danach": plan["battery_action"][j] if j is not None else None,
        }


class EmsNextStartSensor(EmsPlanBase):
    """Nächster geplanter Start (Warmwasser, Auto)."""

    _attr_device_class = "timestamp"

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, key: str, name: str, series: str) -> None:
        super().__init__(coordinator, entry, key, name)
        self._series = series

    @property
    def native_value(self) -> datetime | None:
        i = self.coordinator.plan_index()
        if i is None:
            return None
        j = next_start(self.coordinator.plan[self._series], i)
        return None if j is None else self.coordinator.plan["slots"][j]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        i = self.coordinator.plan_index()
        if i is None:
            return {}
        plan = self.coordinator.plan
        attrs: dict[str, Any] = {"geplant_kwh": round(sum(plan[self._series][i:]) * SLOT_H, 2)}
        if self._series == "car_kw":
            attrs["davon_netz_kwh"] = round(sum(plan["car_grid_kw"][i:]) * SLOT_H, 2)
            attrs.update(plan["car"])
        return attrs


def _days(period: str) -> list[str]:
    today = dt_util.now().date()
    if period == "today":
        return [today.isoformat()]
    first = today - timedelta(days=today.weekday()) if period == "week" else today.replace(day=1)
    return [(first + timedelta(days=n)).isoformat() for n in range((today - first).days + 1)]


def _fee(coordinator: EmsCoordinator, days: list[str]) -> float:
    """Grundgebühr für die Tage ab Beginn der Zählung."""
    first = min(coordinator.costs) if coordinator.costs else dt_util.now().date().isoformat()
    return round(sum(1 for d in days if d >= first) * coordinator.params.fixed_fee_eur_day, 2)


class EmsCostSensor(EmsPlanBase):
    """Tatsächliche Stromkosten: Netzbezug zum Tibber-Endpreis plus Grundgebühr je Tag."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = "monetary"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, key: str, name: str, period: str) -> None:
        super().__init__(coordinator, entry, key, name)
        self._period = period

    @property
    def native_value(self) -> float:
        days = _days(self._period)
        return round(self.coordinator.cost_sum(days) + _fee(self.coordinator, days), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        days = _days(self._period)
        return {
            "bezug_kwh": self.coordinator.cost_sum(days, "kwh"),
            "davon_bezug_eur": round(self.coordinator.cost_sum(days), 2),
            "davon_grundgebuehr_eur": _fee(self.coordinator, days),
        }


class EmsCostForecastSensor(EmsPlanBase):
    """Voraussichtliche Kosten: heute = bisher angefallen plus geplanter Rest."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = "monetary"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry, key: str, name: str, offset: int) -> None:
        super().__init__(coordinator, entry, key, name)
        self._offset = offset

    @property
    def native_value(self) -> float | None:
        plan = self.coordinator.plan
        i = self.coordinator.plan_index()
        if not plan or i is None:
            return None
        day = dt_util.now().date() + timedelta(days=self._offset)
        rest = sum(
            max(0.0, plan["grid_kw"][n]) * SLOT_H * plan["price_ct"][n] / 100.0
            for n in range(i, len(plan["slots"]))
            if plan["slots"][n].date() == day
        )
        done = self.coordinator.cost_sum([day.isoformat()]) if self._offset == 0 else 0.0
        return round(done + rest + self.coordinator.params.fixed_fee_eur_day, 2)


class EmsImportSensor(EmsPlanBase):
    """Netzbezug heute in kWh (eigene Messung aus Netzleistung)."""

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = "energy"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "import_today", "Netzbezug heute")

    @property
    def native_value(self) -> float:
        return self.coordinator.cost_sum(_days("today"), "kwh")


class EmsHeatModelSensor(EmsPlanBase):
    """Heizkennwert: elektrische Leistung je Kelvin unter der Heizgrenze."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "W/K"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "heat_w_per_k", "Heizkennwert")

    @property
    def native_value(self) -> float:
        return round(self.coordinator.heat_w_per_k, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tage_im_fit": self.coordinator.heat_fit_days,
            "heizgrenze_c": self.coordinator.params.heat_limit_c,
            "grundlast_profil_stunden": len(self.coordinator.base_profile),
        }


class EmsExecutorSensor(EmsPlanBase):
    """Was der Ausführer tun würde bzw. tut."""

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "executor", "Ausführer")

    @property
    def native_value(self) -> str:
        if self.coordinator.intent.get("grund"):
            return self.coordinator.intent["grund"]
        return "steuert" if self.coordinator.switches["master"] else "Schattenbetrieb"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        intent = self.coordinator.intent
        car = intent.get("car")
        return {
            "speicher": intent.get("battery"),
            "speicher_sollwert_w": intent.get("battery_w"),
            "warmwasser_laden": intent.get("dhw"),
            "heizung": intent.get("heat"),
            "heizung_hinweis": intent.get("heat_grund"),
            "wallbox": car,
            "geschrieben": self.coordinator.last_written,
        }


class EmsHeatPlanSensor(EmsPlanBase):
    """Geplanter Heizungsmodus im laufenden Slot und die nächsten Sperren."""

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "plan_heating", "Plan Heizung")

    @property
    def native_value(self) -> str | None:
        i = self.coordinator.plan_index()
        if i is None or not self.coordinator.plan.get("heat_mode"):
            return None
        return self.coordinator.plan["heat_mode"][i]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan or {}
        log = self.coordinator.block_log
        return {
            "geplante_sperren": [{k: v for k, v in b.items() if not k.endswith("_index")} for b in plan.get("heat_blocks", [])],
            "sperre_erlaubt": self.coordinator.params.heat_block_enabled,
            "protokoll_anzahl": len(log),
            "protokoll_letzte": log[-5:],
        }


class EmsProactiveSensor(EmsPlanBase):
    """Vorausschau: empfohlene Verschiebung der Komforttemperatur und erwartete Raumtemperatur."""

    _attr_native_unit_of_measurement = "K"
    _attr_icon = "mdi:crystal-ball"

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "heat_proactive", "Heizung Vorausschau")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.proactive.get("shift_k")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        adv = self.coordinator.proactive
        return {
            "grund": adv.get("grund"),
            "raum_erwartet_c": adv.get("predicted_c"),
            "raum_trend_k_je_h": None if adv.get("slope_k_h") is None else round(adv["slope_k_h"], 3),
            "aussen_zuletzt_c": None if adv.get("out_past_c") is None else round(adv["out_past_c"], 1),
            "aussen_voraus_c": adv.get("out_ahead_c"),
            "komfort_soll_c": adv.get("comfort_target_c"),
            "komfort_ist_c": adv.get("comfort_now_c"),
            "heizkurve_ist": adv.get("curve_now"),
            "heizkurve_empfohlen": adv.get("curve_target"),
            "sommerbetrieb": adv.get("sommerbetrieb"),
        }


class EmsTripDestinationSensor(EmsPlanBase):
    """Adresse, für die gerade die Strecke abgefragt wird. Dient dem Fahrzeit-Sensor als Ziel."""

    _attr_icon = "mdi:map-marker"

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "trip_destination", "Fahrtziel")

    @property
    def native_value(self) -> str | None:
        # vor der ersten Abfrage das Zuhause, damit der Fahrzeit-Dienst gültige Koordinaten hat
        home = f"{self.coordinator.hass.config.latitude},{self.coordinator.hass.config.longitude}"
        return self.coordinator.trip_destination or home


class EmsTripSensor(EmsPlanBase):
    """Nächste Abfahrt aus dem Kalender und der Ladebedarf dafür."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:car-clock"

    def __init__(self, coordinator: EmsCoordinator, entry: EmsConfigEntry) -> None:
        super().__init__(coordinator, entry, "trip_next", "Nächste Fahrt")

    @property
    def native_value(self) -> datetime | None:
        trips = (self.coordinator.plan or {}).get("trips") or []
        return datetime.fromisoformat(trips[0]["abfahrt"]) if trips else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        trips = (self.coordinator.plan or {}).get("trips") or []
        return {
            "status": self.coordinator.trip_status,
            "fahrten": [{k: v for k, v in t.items() if k != "address"} for t in trips],
            "bekannte_strecken": len(self.coordinator.routes),
            "spaetere_fahrten": self.coordinator.later_trips,
            "bedarf_7_tage_kwh": round(
                sum(t["kwh"] for t in trips) + sum(t["kwh"] for t in self.coordinator.later_trips), 1
            ),
            "fehlt_kwh": self.coordinator.charge_needed_kwh(),
            "verbrauch_kwh_100km": self.coordinator.car_consumption,
            "verbrauch_basis_km": self.coordinator.car_consumption_km,
        }
