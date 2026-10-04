"""Tests für die Plangenauigkeit."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.ems_mw4 import accuracy as acc
from custom_components.ems_mw4.forecast import build_slots

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def _plan(created: datetime, pv: float = 2.0, base: float = 0.4):
    slots = build_slots(created.astimezone(BERLIN))
    return slots, [pv] * len(slots), [base] * len(slots)


def _key(moment: datetime) -> str:
    return acc.hour_key(moment)


def test_forecast_lead_rules() -> None:
    state = acc.new_state()
    created = T0 + timedelta(minutes=15, seconds=20)
    slots, pv, base = _plan(created)
    acc.record_forecast(state, created, slots, pv, base, 0.25)
    # unter 6 Stunden Vorlauf und die angebrochene Stunde: nichts
    assert _key(T0) not in state["fc"]
    assert _key(T0 + timedelta(hours=6)) not in state["fc"]
    assert state["fc"][_key(T0 + timedelta(hours=7))]["pv"] == 2.0
    assert state["fc"][_key(T0 + timedelta(hours=7))]["base"] == 0.4
    # spätere Prognose: 6..12 h bleibt die älteste, ab 12 h gewinnt die neuere
    later = created + timedelta(hours=1)
    slots, pv, base = _plan(later, pv=5.0)
    acc.record_forecast(state, later, slots, pv, base, 0.25)
    assert state["fc"][_key(T0 + timedelta(hours=7))]["pv"] == 2.0
    assert state["fc"][_key(T0 + timedelta(hours=13))]["pv"] == 2.0  # jetzt 11,7 h Vorlauf
    assert state["fc"][_key(T0 + timedelta(hours=14))]["pv"] == 5.0  # 12,7 h Vorlauf


def test_forecast_ends_at_twelve_hours() -> None:
    """Bei lückenlosem Betrieb steht am Ende die Prognose von 12 h bis 12 h 15 vorher."""
    state = acc.new_state()
    target = T0 + timedelta(hours=30)
    created = T0 + timedelta(seconds=20)
    while created < target:
        slots, pv, base = _plan(created)
        acc.record_forecast(state, created, slots, pv, base, 0.25)
        created += timedelta(minutes=15)
    lead = target - datetime.fromisoformat(state["fc"][_key(target)]["t"])
    assert timedelta(hours=12) <= lead <= timedelta(hours=12, minutes=15)


def test_forecast_dst_change() -> None:
    """Zeitumstellung am 25.10.2026: keine Stunde mit falscher Slotzahl."""
    state = acc.new_state()
    created = datetime(2026, 10, 24, 10, 0, 20, tzinfo=UTC)
    slots, pv, base = _plan(created)
    acc.record_forecast(state, created, slots, pv, base, 0.25)
    assert all(v["pv"] == 2.0 for v in state["fc"].values())
    assert len(state["fc"]) >= 38


def _run(state, start: datetime, minutes: int, pv=3000.0, home=2500.0, hp=1500.0, wb=500.0, step=60):
    now = start
    for _ in range(minutes * 60 // step + 1):
        acc.track(state, now, pv, home, hp, wb)
        now += timedelta(seconds=step)
    return now


def test_actual_full_hour() -> None:
    state = acc.new_state()
    _run(state, T0 - timedelta(minutes=1), 62)
    ist = state["ist"][_key(T0)]
    assert abs(ist["pv"] - 3.0) < 1e-6
    assert abs(ist["base"] - 0.5) < 1e-6  # 2500 - 1500 - 500 W
    assert _key(T0 - timedelta(hours=1)) not in state["ist"]  # nur eine Minute gemessen


def test_actual_restart_and_gaps() -> None:
    # kurzer Neustart (4 Minuten Lücke): Stunde zählt, hochgerechnet
    state = acc.new_state()
    now = _run(state, T0, 20)
    state = acc.clean_state(dict(state))  # wie nach dem Laden aus dem Speicher
    _run(state, now + timedelta(minutes=4), 40)
    assert abs(state["ist"][_key(T0)]["pv"] - 3.0) < 1e-6
    # langer Ausfall: Stunde zählt nicht
    state = acc.new_state()
    now = _run(state, T0, 20)
    _run(state, now + timedelta(minutes=15), 30)
    assert _key(T0) not in state["ist"]
    # fehlender Messwert über 10 Minuten: Stunde zählt nicht
    state = acc.new_state()
    now = _run(state, T0, 20)
    now = _run(state, now, 10, hp=None)
    _run(state, now, 35)
    assert _key(T0) not in state["ist"]
    # mehrstündiger Ausfall: kein Wert für die Zwischenstunden, kein Absturz
    state = acc.new_state()
    now = _run(state, T0, 5)
    _run(state, now + timedelta(hours=5), 5)
    assert state["ist"] == {}


def _filled(hours: int, fc_pv: float, ist_pv: float, fc_base: float, ist_base: float):
    state = acc.new_state()
    for n in range(hours):
        key = _key(T0 - timedelta(hours=n + 1))
        state["fc"][key] = {"t": "x", "pv": fc_pv, "base": fc_base}
        state["ist"][key] = {"pv": ist_pv, "base": ist_base}
    return state


def test_evaluate_learning_and_ok() -> None:
    result = acc.evaluate(_filled(71, 1.0, 1.0, 0.5, 0.5), T0)
    assert result["wert"] is None and result["status"] == "lernt" and result["bewertete_stunden"] == 71
    result = acc.evaluate(_filled(72, 1.2, 1.0, 0.4, 0.5), T0)
    assert result["status"] == "ok"
    assert result["pv_genauigkeit"] == 80.0
    assert result["verbrauch_genauigkeit"] == 80.0
    assert result["wert"] == 80
    # gewichtet nach Energie: PV 3 kWh je Stunde zu 90 %, Verbrauch 1 kWh zu 50 %
    result = acc.evaluate(_filled(72, 2.7, 3.0, 0.5, 1.0), T0)
    assert result["wert"] == 80


def test_evaluate_night_and_floor() -> None:
    # Nachtstunden mit PV 0 stören nicht
    state = _filled(80, 0.0, 0.0, 0.5, 0.5)
    result = acc.evaluate(state, T0)
    assert result["pv_genauigkeit"] is None and result["wert"] == 100
    # Fehler größer als der Istwert: 0, nicht negativ
    assert acc.evaluate(_filled(72, 5.0, 1.0, 0.5, 0.5), T0)["pv_genauigkeit"] == 0.0


def test_window_and_prune() -> None:
    state = _filled(8 * 24 + 10, 1.0, 1.0, 0.5, 0.5)
    assert acc.evaluate(state, T0)["bewertete_stunden"] == 7 * 24
    state["kosten_fc"] = {"2026-09-20": 1.0, "2026-10-04": 2.0}
    acc.prune(state, T0)
    assert len(state["ist"]) == 8 * 24 and len(state["fc"]) == 8 * 24
    assert state["kosten_fc"] == {"2026-10-04": 2.0}


def test_cost_forecast() -> None:
    state = acc.new_state()
    costs = {"2026-10-05": 1.0, "2026-10-06": 2.0}
    acc.record_cost_forecast(state, datetime(2026, 10, 5, 9, 0, tzinfo=BERLIN), costs)
    acc.record_cost_forecast(state, datetime(2026, 10, 5, 11, 45, tzinfo=BERLIN), {"2026-10-06": 3.0})
    acc.record_cost_forecast(state, datetime(2026, 10, 5, 12, 0, tzinfo=BERLIN), {"2026-10-06": 4.0})
    assert state["kosten_fc"] == {"2026-10-06": 3.0}


def test_clean_state() -> None:
    assert acc.clean_state(None) == acc.new_state()
    assert acc.clean_state({"fc": [], "acc": {"h": "x"}}) == acc.new_state()


async def test_sensor_and_restart(hass, hass_storage) -> None:
    """Sensor wird angelegt, zeigt in der Anlaufphase unknown/lernt, der Stand überlebt einen Neustart."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ems_mw4.const import ACCURACY_STORE_KEY, DOMAIN, SOURCES

    for s in SOURCES:
        hass.states.async_set(s.default, "on" if s.default.startswith("binary_sensor") else "2")
    entry = MockConfigEntry(domain=DOMAIN, title="EMS MW4", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.ems_mw4_plangenauigkeit")
    assert state.state == "unknown"
    assert state.attributes["status"] == "lernt"
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["state_class"] == "measurement"
    for name in ("pv_genauigkeit", "verbrauch_genauigkeit", "bewertete_stunden", "zeitraum_tage", "status",
                 "letzte_bewertung", "kosten_prognose_gestern", "kosten_ist_gestern"):
        assert name in state.attributes
    coordinator = entry.runtime_data
    now = datetime.now(UTC)
    for n in range(80):
        key = acc.hour_key(now - timedelta(hours=n + 1))
        coordinator.accuracy["fc"][key] = {"t": "x", "pv": 0.9, "base": 0.5}
        coordinator.accuracy["ist"][key] = {"pv": 1.0, "base": 0.5}
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass_storage[ACCURACY_STORE_KEY]["data"]["fc"]) == 80
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.ems_mw4_plangenauigkeit")
    assert state.state == "93"  # (90 % * 80 kWh + 100 % * 40 kWh) / 120 kWh
    assert state.attributes["status"] == "ok" and state.attributes["bewertete_stunden"] == 80
