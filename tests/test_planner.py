"""Tests für Prognose und Planer (reine Funktionen)."""

from datetime import datetime, timedelta, timezone
import time

from custom_components.ems_mw4.const import SLOT_H, Params
from custom_components.ems_mw4 import forecast as fc
from custom_components.ems_mw4.planner import build_plan, plan_battery, plan_car_grid, plan_dhw

P = Params()
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
SLOTS = fc.build_slots(T0)
N = len(SLOTS)


def _energy_balance(plan, net):
    # Netz = Bedarf - Speicherabgabe
    for i in range(N):
        assert abs(plan["grid_kw"][i] - (net[i] - plan["battery_kw"][i])) < 1e-6


def test_slots():
    assert N == 192 and SLOTS[1] - SLOTS[0] == timedelta(minutes=15)
    assert fc.slot_start(datetime(2026, 10, 5, 10, 44, 59, tzinfo=timezone.utc)).minute == 30


def test_price_series_fills_unknown():
    known = {s: 30.0 + s.hour for s in SLOTS[:96]}
    prices, est = fc.price_series(SLOTS, known)
    assert prices[0] == 30.0 and not est[0]
    assert est[100] and prices[100] == prices[4]
    prices, est = fc.price_series(SLOTS, {})
    assert prices[0] is None


def test_pv_series():
    hours = {SLOTS[0].date(): {"12:00": 4.0, "13:00": "x"}}
    pv = fc.pv_series(SLOTS, hours)
    assert pv[48] == 4.0 and pv[52] == 0.0 and pv[96 + 48] == 0.0


def test_base_load_and_heat_fit():
    rows = [(T0 + timedelta(hours=h), 300.0 + (h % 24) * 10) for h in range(24 * 14)]
    prof = fc.base_load_profile(rows)
    series = fc.base_load_series(SLOTS, prof, 350.0)
    assert abs(series[0] - 0.300) < 1e-9 and abs(series[4 * 5] - 0.350) < 1e-9
    assert fc.base_load_series(SLOTS, {}, 350.0)[0] == 0.35
    k, n = fc.fit_heat([(5.0, 800.0)] * 20, 15.0, 60.0)
    assert n == 20 and abs(k - 80.0) < 1e-6
    k, n = fc.fit_heat([(5.0, 800.0)] * 3, 15.0, 60.0)
    assert k == 60.0
    assert fc.heat_series([5.0, 20.0, None], 15.0, 60.0) == [0.6, 0.0, 0.0]


def test_battery_flat_price_discharges_and_never_exports():
    prices = [30.0] * N
    net = [0.5] * N
    plan = plan_battery(prices, net, 50.0, P)
    _energy_balance(plan, net)
    assert plan["battery_kw"][0] > 0.4  # deckt Hausbedarf
    assert min(plan["grid_kw"]) >= -1e-6  # keine Einspeisung aus dem Speicher
    assert min(plan["soc"]) >= P.battery_min_soc - 0.01
    assert max(plan["battery_kw"]) <= P.battery_discharge_kw + 1e-6


def test_battery_grid_charges_only_with_real_spread():
    # Nacht 25 ct, Tag 45 ct: lohnt (45*0.906-2 > 25/0.906)
    prices = [25.0 if s.hour < 6 else 45.0 for s in SLOTS]
    net = [0.8] * N
    plan = plan_battery(prices, net, 5.0, P)
    _energy_balance(plan, net)
    night = [plan["battery_kw"][i] for i, s in enumerate(SLOTS) if s.hour < 6 and i < 96]
    assert min(night) < -1.0  # lädt nachts aus dem Netz
    assert max(plan["soc"]) > 60
    # Spreizung zu klein: 30 gegen 34 ct lohnt nicht
    prices = [30.0 if s.hour < 6 else 34.0 for s in SLOTS]
    plan = plan_battery(prices, net, 5.0, P)
    assert min(plan["battery_kw"]) > -0.05


def test_battery_saves_for_peak():
    # teure Abendspitze: mittags nicht leerfahren, wenn es nicht für beides reicht
    prices = [60.0 if 18 <= s.hour < 21 else 32.0 for s in SLOTS]
    net = [1.5] * N
    plan = plan_battery(prices, net, 30.0, P)
    peak = [plan["grid_kw"][i] for i, s in enumerate(SLOTS[:96]) if 18 <= s.hour < 21]
    assert max(peak) < 0.3  # Spitze aus dem Speicher


def test_battery_pv_charge_limits():
    prices = [35.0] * N
    net = [-8.0 if 11 <= s.hour < 15 else 0.4 for s in SLOTS]
    plan = plan_battery(prices, net, 10.0, P)
    _energy_balance(plan, net)
    assert min(plan["battery_kw"]) >= -P.battery_charge_kw - 1e-6
    assert max(plan["soc"]) <= 100.0


def test_dhw_one_per_day_cheapest_and_skip():
    prices = [40.0] * N
    for i in range(52, 56):
        prices[i] = 20.0
    dhw = plan_dhw(SLOTS, prices, [0.5] * N, 40.0, P)
    assert sum(1 for v in dhw[:96] if v) == 3 and all(dhw[i] for i in (52, 53, 54))
    assert sum(1 for v in dhw[96:] if v) == 3
    dhw = plan_dhw(SLOTS, prices, [0.5] * N, 55.0, P)
    assert sum(dhw[:96]) == 0 and sum(dhw[96:]) > 0
    # PV-Überschuss schlägt günstigen Netzpreis
    net = [0.5] * N
    for i in range(48, 52):
        net[i] = -4.0
    dhw = plan_dhw(SLOTS, prices, net, 40.0, P)
    assert dhw[48] > 0 and dhw[52] == 0


def test_car_grid_rules():
    prices = [40.0] * N
    for i in range(8, 12):
        prices[i] = 20.0
    hist = [40.0] * 900 + [20.0] * 60
    out, info = plan_car_grid(SLOTS, prices, True, 60.0, hist, P)
    assert info["schwelle_ct"] == 32.75
    assert all(out[i] > 0 for i in range(8, 12)) and sum(1 for v in out if v) == 4
    # flache Preise: kein Netzladen
    out, info = plan_car_grid(SLOTS, [40.0] * N, True, 60.0, [39.0, 40.0, 41.0] * 300, P)
    assert sum(out) == 0
    # Grundreserve: unter 20 % wird bis 7 Uhr geladen, auch teuer
    out, info = plan_car_grid(SLOTS, [40.0] * N, True, 10.0, [40.0] * 900, P)
    kwh = sum(out) * SLOT_H
    assert abs(kwh - 6.47) < 0.01 and all(v == 0 for v in out[28:])
    # nicht angesteckt oder Ladestand unbekannt
    assert sum(plan_car_grid(SLOTS, prices, False, 10.0, hist, P)[0]) == 0
    assert sum(plan_car_grid(SLOTS, prices, True, None, hist, P)[0]) == 0


def test_build_plan_complete_and_fast():
    prices = [28.0 + 15.0 * (1 if 17 <= s.hour < 21 else 0) for s in SLOTS]
    pv = [5.0 if 10 <= s.hour < 15 else 0.0 for s in SLOTS]
    start = time.perf_counter()
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.3] * N, 40.0, 45.0, True, 70.0, prices, P)
    assert time.perf_counter() - start < 5.0
    for key in ("price_ct", "pv_kw", "dhw_kw", "car_kw", "battery_kw", "soc", "grid_kw", "battery_action"):
        assert len(plan[key]) == N
    assert set(plan["battery_action"]) <= {"Netzladen", "PV-Laden", "Entladen", "Halten", "Leerlauf"}
    assert sum(plan["car_kw"]) > 0  # Auto bekommt PV-Überschuss
    assert all(v >= 0 for v in plan["cost_eur"].values())
    # Auto-PV-Laden zieht nie Netzstrom
    for i in range(N):
        if plan["car_kw"][i] > 0 and plan["car_grid_kw"][i] == 0:
            assert plan["grid_kw"][i] <= 1e-6


def test_battery_charges_pv_early_on_tie():
    prices = [35.0] * N
    net = [-3.0 if 10 <= s.hour < 17 else 0.3 for s in SLOTS]
    plan = plan_battery(prices, net, 30.0, P)
    first_surplus = next(i for i, s in enumerate(SLOTS) if s.hour == 10)
    assert plan["battery_kw"][first_surplus] < -2.0  # lädt sofort, nicht erst am Nachmittag


def test_car_pv_only_after_battery_half_full():
    prices = [40.0] * N  # flach: kein Netzladen des Autos
    pv = [6.0 if 9 <= s.hour < 16 else 0.0 for s in SLOTS]
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 10.0, 55.0, True, 60.0, prices, P)
    first_car = next(i for i, v in enumerate(plan["car_kw"]) if v > 0)
    assert plan["soc"][first_car - 1] >= P.car_after_battery_soc - 1.0  # Speicher zuerst bis 50 %
    assert plan["battery_kw"][36] < -1.0  # 9 Uhr: Speicher lädt, Auto noch nicht
    assert plan["car_kw"][36] == 0
    for i in range(N):
        if plan["car_kw"][i] > 0:
            assert plan["grid_kw"][i] <= 0.1  # Auto zieht keinen Netzstrom
    # Speicher schon über 50 %: Auto bekommt den Überschuss sofort
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 70.0, 55.0, True, 60.0, prices, P)
    assert plan["car_kw"][36] > 0


def test_car_no_grid_charging_when_pv_covers():
    prices = [40.0] * N
    for i in range(52, 60):
        prices[i] = 20.0  # sehr günstiges Fenster
    hist = [40.0] * 900 + [20.0] * 100
    pv = [6.0 if 9 <= s.hour < 16 else 0.0 for s in SLOTS]
    # viel PV: kein Netzladen trotz günstigem Fenster
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 60.0, 55.0, True, 80.0, hist, P)
    assert plan["car"]["pv_deckt_bedarf"] is True and sum(plan["car_grid_kw"]) == 0
    assert sum(plan["car_kw"]) > 0
    # keine PV: Netzladen im günstigen Fenster
    plan = build_plan(SLOTS, prices, [False] * N, [0.0] * N, [0.4] * N, [0.0] * N, 60.0, 55.0, True, 80.0, hist, P)
    assert plan["car"]["pv_deckt_bedarf"] is False and sum(plan["car_grid_kw"]) > 0
    # Grundreserve gilt immer, auch bei viel PV
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 60.0, 55.0, True, 10.0, hist, P)
    assert sum(plan["car_grid_kw"]) * 0.25 >= 6.4


# ---------- Heizung ----------

from dataclasses import replace  # noqa: E402

from custom_components.ems_mw4.planner import plan_heating  # noqa: E402

PB = replace(P, heat_block_enabled=True)


def _peak_prices():
    return [50.0 if 18 <= s.hour < 20 else 30.0 for s in SLOTS]


def test_heating_no_block_when_disabled_or_frost_or_small_advantage():
    heat = [1.0] * N
    temps = [5.0] * N
    out, modes, blocks = plan_heating(SLOTS, _peak_prices(), temps, heat, 21.0, None, P)
    assert blocks == [] and out == heat and set(modes) == {"normal"}
    # Frost im Fenster oder in den 6 Stunden danach
    frosty = [(-1.0 if 21 <= s.hour < 23 else 5.0) for s in SLOTS]
    assert plan_heating(SLOTS, _peak_prices(), frosty, heat, 21.0, None, PB)[2] == []
    # Preisvorteil unter 8 ct
    weak = [36.0 if 18 <= s.hour < 20 else 30.0 for s in SLOTS]
    assert plan_heating(SLOTS, weak, temps, heat, 21.0, None, PB)[2] == []
    # keine Temperaturprognose: keine Sperre
    assert plan_heating(SLOTS, _peak_prices(), [None] * N, heat, 21.0, None, PB)[2] == []


def test_heating_block_with_preheat():
    heat = [1.0] * N
    out, modes, blocks = plan_heating(SLOTS, _peak_prices(), [5.0] * N, heat, 21.0, None, PB)
    assert len(blocks) == 2  # je Tag die Abendspitze
    b = blocks[0]
    assert b["end_index"] - b["start_index"] <= 12  # höchstens 3 Stunden
    assert b["end_index"] - b["start_index"] == 8  # genau die teuren 2 Stunden
    assert all(modes[i] == "sperre" and out[i] == 0.0 for i in range(b["start_index"], b["end_index"]))
    pre = range(b["start_index"] - 8, b["start_index"])
    assert all(modes[i] == "vorheizen" for i in pre)
    # Energie bleibt erhalten, plus 10 % Vorheizverlust
    assert abs(sum(out[i] for i in pre) - (8 * 1.0 + 8 * 1.0 * 1.1)) < 1e-6
    assert b["preisvorteil_ct"] == 20.0
    # Mindestabstand 6 Stunden zwischen zwei Sperren
    assert blocks[1]["start_index"] - blocks[0]["end_index"] >= 24


def test_heating_block_max_three_hours():
    prices = [50.0 if 16 <= s.hour < 22 else 30.0 for s in SLOTS]  # 6 Stunden teuer
    out, modes, blocks = plan_heating(SLOTS, prices, [5.0] * N, [1.0] * N, 21.0, None, PB)
    assert blocks and all(b["end_index"] - b["start_index"] <= 12 for b in blocks)
    for a, b in zip(blocks, blocks[1:]):
        assert b["start_index"] - a["end_index"] >= 24


def test_quiet_window_shifts_heat():
    out, modes, blocks = plan_heating(SLOTS, [30.0] * N, [5.0] * N, [1.0] * N, 21.0, (92, 108), P)
    assert all(modes[i] == "ruhe" and out[i] == 0 for i in range(92, 108))
    assert all(modes[i] == "vorheizen" for i in range(84, 92))
    assert blocks == []


def test_build_plan_with_heating():
    plan = build_plan(SLOTS, _peak_prices(), [False] * N, [0.0] * N, [0.4] * N, [1.0] * N, 50.0, 55.0, False, None,
                      _peak_prices(), PB, [5.0] * N, 21.0, None)
    assert "sperre" in plan["heat_mode"] and len(plan["heat_blocks"]) == 2
    assert plan["heat_forecast_kw"] == [1.0] * N
