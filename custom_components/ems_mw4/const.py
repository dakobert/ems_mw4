"""Konstanten für EMS MW4."""

from __future__ import annotations

from dataclasses import dataclass

DOMAIN = "ems_mw4"
NAME = "EMS MW4"

UPDATE_INTERVAL_S = 60
SAMPLE_MINUTES = (0, 15, 30, 45)
STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.messreihe"
MAX_SAMPLES = 120 * 96  # 120 Tage im 15-Minuten-Raster
ROOM_MAX_AGE_S = 3 * 3600

CONF_ROOM_SENSORS = "room_sensors"

DEFAULT_ROOM_SENSORS = [
    "sensor.temperatursensor_buro_temperature",
    "sensor.temperatursensor_bad_og_temperature",
    "sensor.temperatursensor_bad_eg_temperature",
    "sensor.temperatursensor_kuche_temperature",
    "sensor.temperatursensor_wohnzimmer_temperature",
]


@dataclass(frozen=True)
class Source:
    """Eine Datenquelle: fremde Entität, die EMS MW4 nur liest."""

    key: str
    name: str
    default: str
    unit: str | None = None
    device_class: str | None = None
    factor: float = 1.0
    debounce: bool = False
    precision: int | None = None


SOURCES: tuple[Source, ...] = (
    Source("pv_power", "PV-Leistung", "sensor.hostname_scb_4fa5bb_sum_power_of_all_pv_dc_inputs", "W", "power", precision=0),
    Source("grid_power", "Netzleistung", "sensor.hostname_scb_4fa5bb_grid_power", "W", "power", precision=0),
    Source("home_power", "Hausverbrauch", "sensor.hostname_scb_4fa5bb_home_power", "W", "power", precision=0),
    Source("battery_power", "Speicherleistung", "sensor.hostname_scb_4fa5bb_battery_power", "W", "power", precision=0),
    Source("battery_soc", "Speicher Ladestand", "sensor.hostname_scb_4fa5bb_battery_soc", "%", "battery", precision=0),
    Source("hp_power", "Wärmepumpe Leistung", "sensor.heizung_stiebel_eltron_wpmsystem_inverter_aufnahmeleistung", "W", "power", factor=1000.0, debounce=True, precision=0),
    Source("dhw_temp", "Warmwasser Temperatur", "sensor.stiebel_eltron_isg_actual_temperature_water", "°C", "temperature", debounce=True, precision=1),
    Source("outdoor_temp", "Außentemperatur", "sensor.stiebel_eltron_isg_outdoor_temperature", "°C", "temperature", debounce=True, precision=1),
    Source("flow_temp", "Heizkreis Temperatur", "sensor.stiebel_eltron_isg_actual_temperature_hk_1", "°C", "temperature", debounce=True, precision=1),
    Source("sg_ready", "SG-Ready-Zustand", "sensor.stiebel_eltron_isg_sg_ready_state", debounce=True, precision=0),
    Source("compressor", "Verdichter", "binary_sensor.stiebel_eltron_isg_compressor", debounce=True, precision=0),
    Source("wallbox_power", "Wallbox Leistung", "sensor.go_echarger_216292_nrg_12", "W", "power", precision=0),
    Source("car_connected", "Auto angesteckt", "binary_sensor.go_echarger_216292_car", precision=0),
    Source("car_soc", "Auto Ladestand", "sensor.ix1_xdrive30_battery_hv_state_of_charge", "%", "battery", precision=0),
    Source("car_mileage", "Auto Kilometerstand", "sensor.ix1_xdrive30_vehicle_mileage", "km", "distance", precision=0),
    Source("price", "Strompreis", "sensor.zuhause_aktueller_strompreis", "ct/kWh", precision=2),
    Source("pv_fc_today", "PV-Prognose heute", "sensor.none_prognose_heute", "kWh", "energy", precision=1),
    Source("pv_fc_tomorrow", "PV-Prognose morgen", "sensor.none_prognose_morgen", "kWh", "energy", precision=1),
    Source("pv_fc_day_after", "PV-Prognose übermorgen", "sensor.none_prognose_ubermorgen", "kWh", "energy", precision=1),
)

SOURCE_BY_KEY = {s.key: s for s in SOURCES}

KEY_ROOM_TEMP = "room_temp"
KEY_SOURCES_OK = "sources_ok"
KEY_SAMPLES = "samples"
