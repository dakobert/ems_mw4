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

# --- Phase 2: Prognose, Planer, Kosten ---
SLOT_MIN = 15
SLOT_H = SLOT_MIN / 60
HORIZON_SLOTS = 48 * 4
PLAN_MINUTES = (0, 15, 30, 45)
COST_STORE_KEY = f"{DOMAIN}.kosten"
ACCURACY_STORE_KEY = f"{DOMAIN}.plangenauigkeit"

CONF_WEATHER = "weather_entity"
DEFAULT_WEATHER = "weather.menslage"
PV_FORECAST_KEYS = ("pv_fc_today", "pv_fc_tomorrow", "pv_fc_day_after")

KEY_PLAN = "plan"


@dataclass(frozen=True)
class Params:
    """Startwerte des Planers. Werden in Phase 3 zu Einstellungen."""

    battery_kwh: float = 12.5
    battery_min_soc: float = 5.0
    battery_charge_kw: float = 5.0
    battery_charge_kw_high: float = 3.5  # oberhalb von 80 %
    battery_discharge_kw: float = 5.0
    battery_eff_charge: float = 0.906  # hin und zurück rund 82 %
    battery_eff_discharge: float = 0.906
    battery_wear_ct: float = 2.0  # je entladener kWh
    battery_step_kwh: float = 0.02
    feed_in_ct: float = 6.9
    heat_limit_c: float = 15.0
    heat_w_per_k: float = 60.0  # elektrisch, Startwert bis genug Daten vorliegen
    dhw_kwh: float = 1.5
    dhw_kw: float = 2.0
    dhw_skip_above_c: float = 48.0
    car_kwh: float = 64.7
    car_kw: float = 11.0
    car_min_kw: float = 1.4
    car_target_soc: float = 100.0
    car_reserve_soc: float = 20.0
    pv_safety: float = 0.7  # Abschlag auf die PV-Prognose bei Auto-Entscheidungen
    car_after_battery_soc: float = 50.0  # PV-Überschuss: erst Speicher bis hier, dann Auto
    car_cheap_quantile: float = 0.10
    car_now: bool = False  # einmalig: sofort mit voller Leistung bis zum Ladeziel laden
    car_kwh_per_100km: float = 20.0
    trip_buffer_min: float = 15.0
    trip_home_stay_min: float = 60.0  # Kette, wenn zwischen zwei Terminen weniger Zeit zu Hause bliebe
    allday_depart_h: float = 7.0  # ganztägige Termine: angenommene Abwesenheit
    allday_back_h: float = 17.0
    trip_min_km: float = 5.0  # näher gelegene Ziele zählen nicht als Fahrt
    car_cheap_below_mean_ct: float = 6.0
    base_load_default_w: float = 350.0
    fixed_fee_eur_day: float = 0.46
    # Heizung
    room_target_c: float = 21.0
    room_band_up_c: float = 1.0
    room_band_down_c: float = 0.5
    heat_block_enabled: bool = False
    heat_block_max_h: float = 3.0
    heat_block_gap_h: float = 6.0
    heat_block_min_adv_ct: float = 8.0
    heat_block_frost_c: float = 0.0
    heat_block_frost_after_h: float = 6.0
    heat_preheat_h: float = 2.0
    heat_preheat_loss: float = 1.1  # Mehrverbrauch durch höhere Vorlauftemperatur beim Vorheizen
    # Vorausschauend heizen (Fußbodenheizung ist träge)
    heat_comfort_base_c: float = 22.0  # Komforttemperatur der Stiebel ohne Verschiebung
    heat_lookahead_h: float = 24.0
    heat_outdoor_coupling: float = 0.15  # K Raum je K Außentemperatur-Änderung über die Vorausschau
    heat_shift_gain: float = 1.5
    heat_shift_max_k: float = 2.0
    heat_shift_min_k: float = 1.0
    heat_curve_min: float = 0.35
    heat_curve_max: float = 0.50

# --- Phase 3/5: Einstellungen und Ausführer ---
EXEC_INTERVAL_S = 30
PLAN_MAX_AGE_S = 45 * 60
MODBUS_HUB = "Kostal-BYD"
MODBUS_SLAVE = 71
MODBUS_BATTERY_SETPOINT = 1034

CONF_SG_INPUT_1 = "switch.stiebel_eltron_isg_sg_ready_input_1"
CONF_SG_INPUT_2 = "switch.stiebel_eltron_isg_sg_ready_input_2"
CONF_GOE_FRC = "select.go_echarger_216292_frc"
CONF_GOE_AMP = "number.go_echarger_216292_amp"
CONF_GOE_PSM = "select.go_echarger_216292_psm"

SWITCH_MASTER = "master"
SWITCH_BATTERY = "auto_battery"
SWITCH_DHW = "auto_dhw"
SWITCH_WALLBOX = "auto_wallbox"
SWITCH_HEATING = "auto_heating"
SWITCH_HEAT_BLOCK = "heat_block"
SWITCH_QUIET = "quiet_once"
SWITCH_PROACTIVE = "heat_proactive"
SWITCH_CURVE = "heat_curve_auto"
SWITCH_CAR_NOW = "car_now"
CONF_COMFORT_TEMP = "number.stiebel_eltron_isg_comfort_temperature_target_hk1"
CONF_HEAT_CURVE = "number.stiebel_eltron_isg_heating_curve_rise_hk1"
CONF_SUMMER_MODE = "binary_sensor.stiebel_eltron_isg_is_in_summer_mode"
COMFORT_WRITE_GAP_S = 3 * 3600
CONF_TRIP_CALENDAR = "calendar.roy_conbus"
CONF_ROUTE_DISTANCE = "sensor.here_travel_time_entfernung"
CONF_ROUTE_DURATION = "sensor.here_travel_time_dauer"
ROUTE_STORE_KEY = f"{DOMAIN}.strecken"
ROUTE_MAX_AGE_DAYS = 30
CURVE_WRITE_GAP_S = 72 * 3600
BLOCK_LOG_STORE_KEY = f"{DOMAIN}.sperren"
NOTIFY_SERVICE = "mobile_app_iphone_von_roy"


@dataclass(frozen=True)
class Setting:
    """Einstellbarer Parameter des Planers."""

    key: str  # Feldname in Params
    name: str
    minimum: float
    maximum: float
    step: float
    unit: str | None
    icon: str


SETTINGS: tuple[Setting, ...] = (
    Setting("battery_min_soc", "Speicher Mindest-Ladestand", 5, 50, 1, "%", "mdi:battery-low"),
    Setting("battery_wear_ct", "Speicher Verschleißansatz", 0, 10, 0.5, "ct/kWh", "mdi:battery-heart-variant"),
    Setting("feed_in_ct", "Einspeisevergütung", 0, 20, 0.1, "ct/kWh", "mdi:transmission-tower-export"),
    Setting("car_target_soc", "Auto Ladeziel", 50, 100, 5, "%", "mdi:car-electric"),
    Setting("car_reserve_soc", "Auto Grundreserve", 0, 50, 5, "%", "mdi:car-battery"),
    Setting("car_cheap_below_mean_ct", "Auto sehr günstig: Abstand zum Mittel", 0, 20, 0.5, "ct/kWh", "mdi:cash-minus"),
    Setting("car_kwh_per_100km", "Auto Verbrauch", 12, 30, 0.5, "kWh/100 km", "mdi:speedometer"),
    Setting("trip_buffer_min", "Fahrt: Puffer vor Abfahrt", 0, 60, 5, "min", "mdi:clock-start"),
    Setting("dhw_kwh", "Warmwasser Energie je Ladung", 0.5, 4, 0.1, "kWh", "mdi:water-boiler"),
    Setting("dhw_skip_above_c", "Warmwasser: heute keine Ladung ab", 38, 60, 1, "°C", "mdi:thermometer-water"),
    Setting("heat_limit_c", "Heizgrenze", 10, 20, 0.5, "°C", "mdi:thermometer-lines"),
    Setting("room_target_c", "Raumtemperatur Soll", 18, 24, 0.5, "°C", "mdi:home-thermometer"),
    Setting("heat_comfort_base_c", "Heizung: Komforttemperatur Grundwert", 18, 25, 0.5, "°C", "mdi:thermometer"),
    Setting("heat_shift_max_k", "Heizung: größte Anhebung", 0, 3, 0.5, "K", "mdi:arrow-up-bold"),
    Setting("heat_curve_min", "Heizkurve: kleinste Steilheit", 0.2, 1.0, 0.05, None, "mdi:chart-bell-curve-cumulative"),
    Setting("heat_curve_max", "Heizkurve: größte Steilheit", 0.2, 1.0, 0.05, None, "mdi:chart-bell-curve-cumulative"),
    Setting("heat_block_max_h", "Sperre: längste Dauer", 0.5, 3, 0.25, "h", "mdi:timer-sand"),
    Setting("heat_block_gap_h", "Sperre: Mindestabstand", 2, 24, 1, "h", "mdi:timer-pause"),
    Setting("heat_block_min_adv_ct", "Sperre: Mindest-Preisvorteil", 2, 30, 0.5, "ct/kWh", "mdi:cash-check"),
    Setting("heat_block_frost_c", "Sperre: keine unter Außentemperatur", -10, 10, 0.5, "°C", "mdi:snowflake-alert"),
)
