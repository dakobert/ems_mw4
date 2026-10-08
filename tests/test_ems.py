"""Tests für das Grundgerüst."""

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ems_mw4.const import DEFAULT_ROOM_SENSORS, DOMAIN, SOURCES
from custom_components.ems_mw4.coordinator import to_number

SG = "sensor.stiebel_eltron_isg_sg_ready_state"


def _fill(hass: HomeAssistant) -> None:
    for s in SOURCES:
        hass.states.async_set(s.default, "on" if s.default.startswith("binary_sensor") else "2")
    for i, e in enumerate(DEFAULT_ROOM_SENSORS):
        hass.states.async_set(e, str(20 + i))


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="EMS MW4", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_to_number() -> None:
    assert to_number("1.5") == 1.5
    assert to_number("on") == 1.0
    assert to_number("off") == 0.0
    for bad in (None, "unavailable", "unknown", "", "abc", "nan", "inf"):
        assert to_number(bad) is None


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT


async def test_values_and_median(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    data = entry.runtime_data.data
    assert data["pv_power"] == 2.0
    assert data["hp_power"] == 2000.0  # kW -> W
    assert data["compressor"] == 1.0
    assert data["room_temp"] == 21.0  # zweitkältester aus 20..24
    assert data["sources_ok"] == len(SOURCES) + 1
    assert hass.states.get("sensor.ems_mw4_raumtemperatur_referenz").state == "21.0"
    assert hass.states.get("sensor.ems_mw4_pv_leistung").state == "2.0"


async def test_missing_never_number(hass: HomeAssistant) -> None:
    _fill(hass)
    hass.states.async_set("sensor.hostname_scb_4fa5bb_battery_soc", "unavailable")
    hass.states.async_remove("sensor.zuhause_aktueller_strompreis")
    entry = await _setup(hass)
    c = entry.runtime_data
    assert c.data["battery_soc"] is None and c.data["price"] is None
    assert set(c.missing) == {"battery_soc", "price"}
    assert hass.states.get("sensor.ems_mw4_speicher_ladestand").state == "unknown"


async def test_room_stale_and_empty(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    future = dt_util.utcnow() + timedelta(hours=4)
    hass.states.async_set(DEFAULT_ROOM_SENSORS[0], "30")  # frisch gemeldet
    value, used = c._room_temperature(dt_util.utcnow())
    assert used == 5
    value, used = c._room_temperature(future)
    assert (value, used) == (None, 0)


async def test_debounce(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    assert c.data["sg_ready"] == 2.0
    hass.states.async_set(SG, "4")
    await c.async_refresh()
    assert c.data["sg_ready"] == 2.0  # einmaliger Sprung zählt nicht
    hass.states.async_set(SG, "2")
    await c.async_refresh()
    assert c.data["sg_ready"] == 2.0
    hass.states.async_set(SG, "3")
    await c.async_refresh()
    await c.async_refresh()
    assert c.data["sg_ready"] == 3.0  # zweimal gleich gilt
    hass.states.async_set(SG, "unavailable")
    await c.async_refresh()
    assert c.data["sg_ready"] is None


async def test_sample_store_and_reload(hass: HomeAssistant, hass_storage) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    c.async_take_sample(dt_util.utcnow())
    assert len(c.samples) == 1 and c.samples[0]["pv_power"] == 2.0
    assert "plan_grid_kw" not in c.samples[0]  # ohne Plan keine Planwerte
    assert c.data["samples"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage["ems_mw4.messreihe"]["data"]["samples"][0]["room_temp"] == 21.0
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.samples) == 1


async def test_options_flow(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    user = {s.key: s.default for s in SOURCES}
    user["pv_power"] = "sensor.andere_pv"
    user["room_sensors"] = DEFAULT_ROOM_SENSORS[:2]
    hass.states.async_set("sensor.andere_pv", "777")
    result = await hass.config_entries.options.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.data["pv_power"] == 777.0
    assert entry.runtime_data.data["room_temp"] == 20.0


# ---------- Phase 2 ----------

from datetime import datetime  # noqa: E402
from unittest.mock import patch  # noqa: E402

BASE = "custom_components.ems_mw4.data"


def _prices(now):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return {start + timedelta(minutes=15 * i): 30.0 + (10.0 if 68 <= i % 96 < 84 else 0.0) for i in range(3 * 96)}


async def _replan(hass, entry, prices=None, temps=None, stats=None):
    now = dt_util.now()
    with (
        patch(f"{BASE}.async_fetch_prices", return_value=_prices(now) if prices is None else prices),
        patch(f"{BASE}.async_fetch_temperatures", return_value=temps or {}),
        patch(f"{BASE}.async_fetch_hourly_means", return_value=stats or {}),
    ):
        await entry.runtime_data.async_replan()
        await hass.async_block_till_done()


async def test_plan_built_and_sensors(hass: HomeAssistant) -> None:
    _fill(hass)
    hass.states.async_set("sensor.hostname_scb_4fa5bb_battery_soc", "40")
    hass.states.async_set("sensor.ix1_xdrive30_battery_hv_state_of_charge", "80")
    hass.states.async_set("sensor.none_prognose_heute", "20", {"hours": {f"{h:02d}:00": 3.0 for h in range(10, 16)}})
    entry = await _setup(hass)
    c = entry.runtime_data
    await c.async_refresh()
    await _replan(hass, entry)
    assert c.plan is not None and len(c.plan["slots"]) == 192
    assert c.plan_status.startswith("ok")
    assert hass.states.get("sensor.ems_mw4_plan").state.startswith("ok")
    assert len(hass.states.get("sensor.ems_mw4_plan").attributes["preis_ct"]) == 192
    battery = hass.states.get("sensor.ems_mw4_plan_speicher")
    assert battery.state in ("Netzladen", "PV-Laden", "Entladen", "Halten", "Leerlauf")
    assert battery.attributes["begruendung"]
    assert hass.states.get("sensor.ems_mw4_netzbezug_kosten_prognose_morgen").state not in ("unknown", "unavailable")


async def test_plan_kept_without_prices_or_soc(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    await _replan(hass, entry)
    first = c.plan
    assert first is not None
    await _replan(hass, entry, prices={})
    assert c.plan is first and "keine Preise" in c.plan_status
    hass.states.async_set("sensor.hostname_scb_4fa5bb_battery_soc", "unavailable")
    await c.async_refresh()
    await _replan(hass, entry)
    assert c.plan is first and "Ladestand" in c.plan_status


async def test_replan_survives_exception(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    with patch(f"{BASE}.async_fetch_prices", side_effect=RuntimeError("kaputt")):
        await entry.runtime_data.async_replan()
    assert entry.runtime_data.plan_status == "Fehler in der Planrechnung"


async def test_cost_tracking(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    t0 = dt_util.utcnow()
    c.costs.clear()
    c._last_cost_time = None
    c._track_cost(t0, 2000.0, 40.0)  # erster Aufruf: nur Zeit merken
    c._track_cost(t0 + timedelta(seconds=60), 2000.0, 40.0)
    day = dt_util.as_local(t0 + timedelta(seconds=60)).date().isoformat()
    assert abs(c.costs[day]["kwh"] - 2.0 / 60) < 1e-4
    assert abs(c.costs[day]["eur"] - 2.0 / 60 * 0.40) < 1e-4
    before = dict(c.costs[day])
    c._track_cost(t0 + timedelta(seconds=120), -3000.0, 40.0)  # Einspeisung kostet nichts
    c._track_cost(t0 + timedelta(seconds=180), None, 40.0)  # fehlender Wert
    c._track_cost(t0 + timedelta(hours=2), 2000.0, 40.0)  # Lücke wird nicht geschätzt
    assert c.costs[day] == before or len(c.costs) == 2
    assert c.cost_sum([day]) == round(before["eur"], 2)


async def test_models_from_stats(hass: HomeAssistant) -> None:
    from custom_components.ems_mw4 import coordinator as coord_mod
    coord_mod.HEAT_DATA_FROM = "2000-01-01"
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=30)
    hours = [start + timedelta(hours=h) for h in range(30 * 24)]
    stats = {
        c.entity_for("home_power"): {h: 1500.0 for h in hours},
        c.entity_for("hp_power"): {h: 0.8 for h in hours},  # kW
        c.entity_for("wallbox_power"): {h: 200.0 for h in hours},
        c.entity_for("outdoor_temp"): {h: 5.0 for h in hours},
    }
    await _replan(hass, entry, stats=stats)
    assert len(c.base_profile) == 48
    assert abs(next(iter(c.base_profile.values())) - 500.0) < 1e-6  # 1500 - 800 - 200
    assert abs(c.heat_w_per_k - 800.0 / 14.0) < 1e-6 and c.heat_fit_days >= 14  # 800 W bei 14 K unter 19 °C


# ---------- Einstellungen und Ausführer ----------

from homeassistant.core import ServiceCall  # noqa: E402
from pytest_homeassistant_custom_component.common import async_mock_service  # noqa: E402

from custom_components.ems_mw4 import executor as ex  # noqa: E402
from custom_components.ems_mw4.const import Params  # noqa: E402


def test_float_words() -> None:
    assert ex.float_words(-500.0) == [0, 50170]  # im Feldtest am 03.10.2026 so geschrieben
    assert ex.float_words(0) == [0, 0]
    assert ex.float_words(-5376.0) == [0, 50600]


def test_car_setpoint() -> None:
    assert ex.car_setpoint(11.0) == ("three_phases", 16)
    assert ex.car_setpoint(4.2) == ("three_phases", 6)
    assert ex.car_setpoint(2.3) == ("one_phase", 10)
    assert ex.car_setpoint(1.4) == ("one_phase", 6)


def _mini_plan(action, batt_kw, dhw=0.0, car=0.0):
    return {"battery_action": [action], "battery_kw": [batt_kw], "dhw_kw": [dhw], "car_kw": [car]}


def test_decide() -> None:
    p = Params()
    ok = {"battery_soc": 50.0, "grid_power": 100.0, "dhw_temp": 45.0, "sg_ready": 2.0, "car_connected": 1.0, "wallbox_power": 0.0}
    d = ex.decide(_mini_plan("Netzladen", -3.0), 0, ok, 60, 2700, p)
    assert d["battery_w"] == -3000 and d["grund"] is None
    assert ex.decide(_mini_plan("Netzladen", -9.0), 0, ok, 60, 2700, p)["battery_w"] == -5000  # gedeckelt
    assert ex.decide(_mini_plan("Halten", 0.0), 0, ok, 60, 2700, p)["battery_w"] == 0
    for action in ("PV-Laden", "Entladen", "Leerlauf"):
        assert ex.decide(_mini_plan(action, 1.0), 0, ok, 60, 2700, p)["battery_w"] is None  # Kostal regelt
    # fehlende Messwerte, alter oder fehlender Plan: nichts schreiben
    assert ex.decide(_mini_plan("Netzladen", -3.0), 0, {**ok, "battery_soc": None}, 60, 2700, p)["battery_w"] is None
    assert ex.decide(_mini_plan("Netzladen", -3.0), 0, ok, 5000, 2700, p)["grund"] == "Plan veraltet"
    assert ex.decide(None, None, ok, None, 2700, p)["grund"] == "kein Plan"
    d = ex.decide(_mini_plan("Leerlauf", 0.0, dhw=2.0, car=11.0), 0, ok, 60, 2700, p)
    assert d["dhw"] is True and d["car"] == {"frc": "charge", "psm": "three_phases", "amp": 16}
    d = ex.decide(_mini_plan("Leerlauf", 0.0), 0, ok, 60, 2700, p)
    assert d["dhw"] is False and d["car"] == {"frc": "dont_charge"}
    d = ex.decide(_mini_plan("Leerlauf", 0.0, car=11.0), 0, {**ok, "car_connected": 0.0, "dhw_temp": None}, 60, 2700, p)
    assert d["car"] is None and d["dhw"] is None


async def test_executor_shadow_writes_nothing(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    calls = async_mock_service(hass, "modbus", "write_register")
    sw = async_mock_service(hass, "switch", "turn_on")
    await _replan(hass, entry)
    c.plan["battery_action"] = ["Netzladen"] * 192
    c.plan["battery_kw"] = [-3.0] * 192
    await c.async_execute()
    assert c.switches["master"] is False
    assert calls == [] and sw == []
    assert c.intent["battery_w"] == -3000
    assert hass.states.get("sensor.ems_mw4_ausfuhrer").state == "Schattenbetrieb"
    assert hass.states.get("switch.ems_mw4_steuerung_aktiv").state == "off"


async def test_executor_active_writes_battery_and_respects_device_switch(hass: HomeAssistant) -> None:
    _fill(hass)
    hass.states.async_set("sensor.hostname_scb_4fa5bb_battery_soc", "50")
    entry = await _setup(hass)
    c = entry.runtime_data
    await c.async_refresh()
    calls = async_mock_service(hass, "modbus", "write_register")
    await _replan(hass, entry)
    c.plan["battery_action"] = ["Halten"] * 192
    c.plan["dhw_kw"] = [0.0] * 192
    c.plan["car_kw"] = [0.0] * 192
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.ems_mw4_steuerung_aktiv"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ems_mw4_automatik_wallbox"}, blocking=True)
    await c.async_execute()
    assert len(calls) == 1
    assert calls[0].data == {"hub": "Kostal-BYD", "slave": 71, "address": 1034, "value": [0, 0]}
    assert c.last_written == {"battery_w": 0, "sg_ready": 2, "dhw": False, "dhw_soll_c": 40.0}
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ems_mw4_automatik_speicher"}, blocking=True)
    await c.async_execute()
    assert len(calls) == 1  # Speicher-Automatik aus: kein weiterer Befehl


async def test_setting_changes_params(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    with patch(f"{BASE}.async_fetch_prices", return_value={}):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.ems_mw4_speicher_mindest_ladestand", "value": 15}, blocking=True
        )
        await hass.async_block_till_done()
    assert c.params.battery_min_soc == 15.0
    assert hass.states.get("number.ems_mw4_speicher_mindest_ladestand").state == "15.0"


# ---------- Heizung ----------


def test_sg_state() -> None:
    assert ex.sg_state(True, "sperre", True, True) == 2  # Warmwasser verhindert die Sperre, lädt aber über den Sollwert
    assert ex.sg_state(False, "sperre", True, True) == 1
    assert ex.sg_state(False, "ruhe", True, True) == 1
    assert ex.sg_state(False, "vorheizen", True, True) == 3
    assert ex.sg_state(False, "normal", True, True) == 2
    assert ex.sg_state(False, "sperre", True, False) == 2  # Heizungs-Automatik aus
    assert ex.sg_state(True, "sperre", False, False) is None  # nichts anfassen
    assert ex.sg_state(None, None, True, True) is None
    assert 4 not in ex.SG_INPUTS  # Zustand 4 wird nie gesetzt
    assert ex.SG_INPUTS[3] == ("on", "off") and ex.SG_INPUTS[1] == ("off", "on")


def test_decide_heating_abort() -> None:
    p = Params()
    base = {"battery_soc": 50.0, "grid_power": 0.0, "sg_ready": 2.0, "dhw_temp": 50.0}
    plan = {"battery_action": ["Leerlauf"], "battery_kw": [0.0], "dhw_kw": [0.0], "car_kw": [0.0], "heat_mode": ["sperre"]}
    assert ex.decide(plan, 0, {**base, "room_temp": 21.0}, 60, 2700, p)["heat"] == "sperre"
    d = ex.decide(plan, 0, {**base, "room_temp": 20.4}, 60, 2700, p)
    assert d["heat"] == "normal" and "abgebrochen" in d["heat_grund"]
    assert ex.decide(plan, 0, {**base, "room_temp": None}, 60, 2700, p)["heat"] == "normal"
    plan["heat_mode"] = ["vorheizen"]
    assert ex.decide(plan, 0, {**base, "room_temp": 21.0}, 60, 2700, p)["heat"] == "vorheizen"
    assert ex.decide(plan, 0, {**base, "room_temp": 22.0}, 60, 2700, p)["heat"] == "normal"


async def test_heating_switches_default_off_and_sg_writes(hass: HomeAssistant) -> None:
    _fill(hass)
    hass.states.async_set("sensor.hostname_scb_4fa5bb_battery_soc", "50")
    for e in DEFAULT_ROOM_SENSORS:
        hass.states.async_set(e, "21.5")
    hass.states.async_set("switch.stiebel_eltron_isg_sg_ready_input_1", "off")
    hass.states.async_set("switch.stiebel_eltron_isg_sg_ready_input_2", "off")
    entry = await _setup(hass)
    c = entry.runtime_data
    await c.async_refresh()
    await c.async_refresh()
    assert hass.states.get("switch.ems_mw4_automatik_heizung").state == "off"
    assert hass.states.get("switch.ems_mw4_sperre_in_preisspitzen").state == "off"
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "modbus", "write_register")
    await _replan(hass, entry)
    c.plan["heat_mode"] = ["sperre"] * 192
    c.plan["dhw_kw"] = [0.0] * 192
    c.plan["car_kw"] = [0.0] * 192
    c.switches["master"] = True
    await c.async_execute()
    assert on == []  # Heizungs-Automatik aus: keine Sperre
    c.switches["auto_heating"] = True
    await c.async_execute()
    assert [x.data["entity_id"] for x in on] == ["switch.stiebel_eltron_isg_sg_ready_input_2"]
    assert c.last_written["sg_ready"] == 1 and c._block_open is not None
    # Sperre endet: Protokolleintrag
    hass.states.async_set("switch.stiebel_eltron_isg_sg_ready_input_2", "on")
    c.plan["heat_mode"] = ["normal"] * 192
    await c.async_execute()
    assert [x.data["entity_id"] for x in off] == ["switch.stiebel_eltron_isg_sg_ready_input_2"]
    assert len(c.block_log) == 1 and c.block_log[0]["raum_start"] == 21.5 and "ende" in c.block_log[0]


async def test_quiet_slots(hass: HomeAssistant) -> None:
    _fill(hass)
    entry = await _setup(hass)
    c = entry.runtime_data
    from custom_components.ems_mw4 import forecast as fc2
    slots = fc2.build_slots(dt_util.now().replace(hour=20, minute=0))
    assert c.quiet_slots(slots) is None
    c.switches["quiet_once"] = True
    start, end = c.quiet_slots(slots)
    assert slots[start].hour == 23 and slots[start].minute == 0
    assert end - start == 16 and slots[end].hour == 3  # 23 bis 3 Uhr, nur die erste Nacht


def test_dhw_target() -> None:
    from custom_components.ems_mw4.const import Params
    p = Params()
    assert ex.dhw_target(False, 45.0, False, p) == (40.0, False)
    assert ex.dhw_target(None, 45.0, True, p) == (40.0, False)  # Fenster vorbei: Merker zurück
    assert ex.dhw_target(True, 50.0, False, p) == (57.0, False)
    assert ex.dhw_target(True, 55.2, False, p) == (40.0, True)  # Schwelle erreicht: fertig
    assert ex.dhw_target(True, 53.0, True, p) == (40.0, True)  # im selben Fenster nicht erneut laden
    assert ex.dhw_target(True, None, False, p) == (57.0, False)


def test_pv_dhw() -> None:
    from custom_components.ems_mw4.const import Params
    p = Params()
    full = {"dhw_temp": 50.0, "battery_soc": 99.0, "grid_power": -2000.0}
    assert ex.pv_dhw(full, None, False, p) == (True, False)  # Start
    assert ex.pv_dhw({**full, "grid_power": -500.0}, None, False, p) == (False, False)  # zu wenig Überschuss
    assert ex.pv_dhw({**full, "battery_soc": 80.0}, None, False, p) == (False, False)  # Speicher nicht voll
    assert ex.pv_dhw(full, None, True, p) == (False, True)  # heute schon erledigt
    assert ex.pv_dhw({**full, "grid_power": 2500.0}, 10.0, False, p) == (True, False)  # läuft weiter trotz Bezug
    assert ex.pv_dhw({**full, "dhw_temp": 55.1}, 10.0, False, p) == (False, True)  # Ziel erreicht
    assert ex.pv_dhw(full, 46.0, False, p) == (False, True)  # Zeit abgelaufen
