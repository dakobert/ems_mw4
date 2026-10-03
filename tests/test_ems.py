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
    assert data["room_temp"] == 22.0  # Median aus 20..24
    assert data["sources_ok"] == len(SOURCES) + 1
    assert hass.states.get("sensor.ems_mw4_raumtemperatur_referenz").state == "22.0"
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
    assert c.data["samples"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage["ems_mw4.messreihe"]["data"]["samples"][0]["room_temp"] == 22.0
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
    assert entry.runtime_data.data["room_temp"] == 20.5


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
    assert abs(c.heat_w_per_k - 80.0) < 1e-6 and c.heat_fit_days >= 14
