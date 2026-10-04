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


# ---------- Vorausschauend heizen ----------

def _room_samples(now, start_temp, slope, out=12.0, hours=24):
    from datetime import timedelta
    return [
        {"t": (now - timedelta(minutes=15 * k)).isoformat(), "room_temp": start_temp - slope * k / 4, "outdoor_temp": out}
        for k in range(hours * 4, -1, -1)
    ]


def test_proactive_raises_early_when_cooling():
    from datetime import datetime, timezone
    from custom_components.ems_mw4 import thermal as th
    from custom_components.ems_mw4.const import Params
    p = Params()
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    samples = _room_samples(now, 21.4, -0.03)  # Raum fällt 0,03 K/h, liegt noch über Soll
    slope = th.room_slope(samples, now, 12)
    assert abs(slope + 0.03) < 0.002
    adv = th.advise(21.4, slope, th.outdoor_past_mean(samples, now, 24), [5.0] * 96, p)
    assert adv["predicted_c"] < p.room_target_c
    assert 0.5 <= adv["shift_k"] <= p.heat_shift_max_k
    # stabil und draußen gleich: keine Verschiebung
    assert th.advise(21.4, 0.0, 12.0, [12.0] * 96, p)["shift_k"] == 0.0
    # zu warm: absenken, höchstens 1 K
    assert th.advise(23.5, 0.02, 12.0, [12.0] * 96, p)["shift_k"] == -1.0
    # über der Heizgrenze und warm genug: nichts tun
    assert th.advise(22.0, -0.01, 18.0, [18.0] * 96, p)["shift_k"] == 0.0
    assert th.advise(None, None, None, [], p)["shift_k"] == 0.0


def test_curve_follows_slowly_within_bounds():
    from custom_components.ems_mw4 import thermal as th
    from custom_components.ems_mw4.const import Params
    p = Params()
    assert th.curve_target(0.40, [1.5] * 288, p) == 0.45
    assert th.curve_target(0.50, [1.5] * 288, p) is None  # Obergrenze
    assert th.curve_target(0.40, [-1.0] * 288, p) == 0.35
    assert th.curve_target(0.35, [-1.0] * 288, p) is None
    assert th.curve_target(0.40, [0.5] * 288, p) is None
    assert th.curve_target(0.40, [1.5] * 50, p) is None  # zu wenig Verlauf


# ---------- Fahrten aus dem Kalender ----------

def test_trips_parse_and_plan():
    from datetime import timedelta
    from custom_components.ems_mw4 import trips as tr
    from custom_components.ems_mw4.planner import plan_car_trips
    now = SLOTS[0]
    start = now + timedelta(hours=20)
    events = [
        {"start": start.isoformat(), "end": (start + timedelta(hours=8)).isoformat(), "summary": "Kurs",
         "location": "Musterstraße 1, 49074 Osnabrück"},
        {"start": start.isoformat(), "end": start.isoformat(), "summary": "ohne Adresse", "location": "Herr Muster"},
        {"start": "2026-10-05", "end": "2026-10-06", "summary": "ganztägig", "location": "Weg 2, 01067 Dresden"},
    ]
    found = [v for v in tr.parse_events(events, now) if v["summary"] == "Kurs"]
    assert len(found) == 1 and tr.is_address("Weg 2, 01067 Dresden") and not tr.is_address("Büro")
    assert tr.with_routes(found, {}, SLOTS, P) == []  # ohne Strecke keine Fahrt
    routes = {found[0]["address"]: {"km": 50.0, "min": 45.0, "t": now.isoformat()}}
    near = {found[0]["address"]: {"km": 4.9, "min": 8.0, "t": now.isoformat()}}
    assert tr.with_routes(found, near, SLOTS, P) == []  # bis 5 km keine Fahrt
    trips = tr.with_routes(found, routes, SLOTS, P)
    assert trips[0]["kwh"] == 20.0 and trips[0]["km"] == 100.0 and trips[0]["dep_index"] == 76  # 20 h - 45 min - 15 min = 19 h
    prices = [40.0] * N
    prices[10], prices[11] = 10.0, 12.0
    prices[100] = 1.0  # billig, aber nach der Abfahrt
    out, away, info = plan_car_trips(prices, True, 30.0, trips, P)
    # Bedarf: 20 kWh + 20 % Reserve (12,94) - vorhanden (19,41) = 13,53 kWh
    assert abs(sum(out) * SLOT_H - 13.53) < 0.01 and info[0]["fehlt_kwh"] == 0.0
    assert out[10] > 0 and out[11] > 0 and out[100] == 0 and all(v == 0 for v in out[76:])
    assert away[76] and away[80] and not away[75]
    # genug geladen: nichts tun; nicht angesteckt: Bedarf melden
    assert sum(plan_car_trips(prices, True, 80.0, trips, P)[0]) == 0
    out, _, info = plan_car_trips(prices, False, 30.0, trips, P)
    assert sum(out) == 0 and info[0]["fehlt_kwh"] == 13.5


def test_learn_consumption():
    from custom_components.ems_mw4 import trips as tr
    # zu wenig Kilometer: eingestellter Wert
    few = [{"car_mileage": 1000.0, "car_soc": 80.0}, {"car_mileage": 1050.0, "car_soc": 65.0}]
    assert tr.learn_consumption(few, 64.7, 20.0) == (20.0, 50.0)
    # 200 km mit 60 % von 64,7 kWh = 19,4 kWh/100 km; Abschnitt mit Laden unterwegs zählt nicht
    data = [
        {"car_mileage": 1000.0, "car_soc": 90.0}, {"car_mileage": 1100.0, "car_soc": 60.0},
        {"car_mileage": 1100.0, "car_soc": 100.0},  # geladen, kein Fahrabschnitt
        {"car_mileage": 1200.0, "car_soc": 70.0},
        {"car_mileage": 1300.0, "car_soc": 80.0},  # unterwegs geladen: herausgefallen
        {"car_mileage": None, "car_soc": 50.0},
    ]
    value, km = tr.learn_consumption(data, 64.7, 20.0)
    assert km == 200.0 and value == 19.4


def test_location_from_microsoft_objects():
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    from custom_components.ems_mw4 import trips as tr
    full = {"displayName": "Musterstraße 1", "address": {"street": "Musterstraße 1", "postalCode": "33602", "city": "Bielefeld"}}
    assert tr.location_text(full) == "Musterstraße 1, 33602 Bielefeld"
    assert tr.location_text({"displayName": "Herr Muster", "address": {}}) == "Herr Muster"
    assert tr.location_text(None) == ""
    t0 = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    objs = [NS(start=t0, end=t0, is_all_day=False, subject="Kurs", location=full),
            NS(start=t0, end=t0, is_all_day=True, subject="Reise", location=full), NS(start=None, end=None)]
    events = tr.events_from_objects(objs)
    assert len(events) == 2 and events[0]["location"].endswith("33602 Bielefeld") and "T" not in events[1]["start"]
    geo = {"displayName": "Halle", "coordinates": {"latitude": 52.02, "longitude": 8.53}}
    assert tr.location_coords(geo) == "52.02,8.53" and tr.location_coords(full) is None
    only = tr.events_from_objects([NS(start=t0, end=t0, is_all_day=False, subject="Kurs", location=geo)])
    trip = tr.parse_events(only, datetime(2026, 10, 3, tzinfo=timezone.utc))[0]
    assert trip["coords"] == "52.02,8.53" and trip["address"] == "Halle" and only[0]["shape"]["koordinaten"]
    assert len(tr.parse_events(events, datetime(2026, 10, 3, tzinfo=timezone.utc))) == 2  # Termin und ganztägig


def test_trip_chain_allday_multiday_overnight():
    from datetime import datetime, timedelta, timezone
    from custom_components.ems_mw4 import trips as tr
    tz = timezone(timedelta(hours=2))
    now = datetime(2026, 10, 3, 12, 0, tzinfo=tz)
    a, b = "52.0,8.0", "52.1,8.0"
    routes = {"A": {"km": 50.0, "min": 40.0, "t": ""}, "B": {"km": 60.0, "min": 50.0, "t": ""}}

    def ev(start, end, loc, coords, desc=""):
        return {"start": start, "end": end, "summary": loc, "location": loc, "coords": coords, "description": desc}

    # Kette: zwei Termine mit 1 h Abstand -> eine Fahrt, hin zu A, weiter zu B, zurück von B
    chain = tr.build_trips(tr.parse_events([
        ev("2026-10-05T09:00:00+02:00", "2026-10-05T11:00:00+02:00", "A", a),
        ev("2026-10-05T12:00:00+02:00", "2026-10-05T14:00:00+02:00", "B", b),
    ], now), routes, P)
    assert len(chain) == 1 and chain[0]["kette"] and chain[0]["summary"] == "A + B"
    hop = tr._hop_km(a, b)
    assert 14 < hop < 15 and abs(chain[0]["km"] - (50 + hop + 60)) < 0.1
    assert chain[0]["abfahrt"] == datetime(2026, 10, 5, 8, 5, tzinfo=tz)
    assert chain[0]["rueckkehr"] == datetime(2026, 10, 5, 14, 50, tzinfo=tz)
    # weit entfernt, 3 h Pause: Heimfahrt lohnt nicht -> Kette
    far = {"A": {"km": 100.0, "min": 70.0, "t": ""}, "B": {"km": 120.0, "min": 80.0, "t": ""}}
    long_gap = [
        ev("2026-10-05T09:00:00+02:00", "2026-10-05T10:00:00+02:00", "A", a),
        ev("2026-10-05T13:00:00+02:00", "2026-10-05T14:00:00+02:00", "B", b),
    ]
    assert len(tr.build_trips(tr.parse_events(long_gap, now), far, P)) == 1
    # nah, 3 h Pause: zwei Fahrten
    two = tr.build_trips(tr.parse_events([
        ev("2026-10-05T09:00:00+02:00", "2026-10-05T10:00:00+02:00", "A", a),
        ev("2026-10-05T13:00:00+02:00", "2026-10-05T14:00:00+02:00", "B", b),
    ], now), routes, P)
    assert len(two) == 2 and not two[0]["kette"] and two[0]["km"] == 100.0
    # ganztägig über drei Tage: je Tag 7 bis 17 Uhr
    days = tr.build_trips(tr.parse_events([ev("2026-10-05", "2026-10-08", "A", a)], now), routes, P)
    assert len(days) == 3 and days[0]["abfahrt"].hour == 7 and days[0]["rueckkehr"].hour == 17
    # mehrtägig mit Uhrzeit: erster Tag ab Beginn, letzter Tag bis Ende
    multi = tr.build_trips(tr.parse_events([ev("2026-10-05T10:00:00+02:00", "2026-10-06T15:00:00+02:00", "A", a)], now), routes, P)
    assert len(multi) == 2 and multi[0]["abfahrt"] == datetime(2026, 10, 5, 9, 5, tzinfo=tz)
    assert multi[0]["rueckkehr"].hour == 17 and multi[1]["abfahrt"].hour == 7
    assert multi[1]["rueckkehr"] == datetime(2026, 10, 6, 15, 40, tzinfo=tz)
    # Übernachtung: eine Fahrt, durchgehend abwesend
    night = tr.build_trips(tr.parse_events([ev("2026-10-05", "2026-10-08", "A", a, "Hotel #Übernachtung")], now), routes, P)
    assert len(night) == 1 and night[0]["km"] == 100.0
    assert night[0]["abfahrt"] == datetime(2026, 10, 5, 7, 0, tzinfo=tz) and night[0]["rueckkehr"] == datetime(2026, 10, 7, 17, 0, tzinfo=tz)
    # ohne Koordinaten und ohne Postleitzahl: keine Fahrt; später als das Planfenster: Liste
    assert tr.parse_events([ev("2026-10-05", "2026-10-06", "Dresden", None)], now) == []
    slots = [now + timedelta(minutes=15 * i) for i in range(192)]
    visits = tr.parse_events([ev("2026-10-08T09:00:00+02:00", "2026-10-08T11:00:00+02:00", "A", a)], now)
    assert tr.with_routes(visits, routes, slots, P) == [] and tr.later_trips(visits, routes, slots, P)[0]["kwh"] == 20.0


def test_car_now_charges_immediately():
    from dataclasses import replace
    prices = [40.0] * N
    out, info = plan_car_grid(SLOTS, prices, True, 60.0, [40.0] * 900, replace(P, car_now=True))
    kwh = sum(out) * SLOT_H
    assert info["sofort"] and abs(kwh - 0.4 * P.car_kwh) < 0.01 and out[0] == P.car_kw
    assert all(v == 0 for v in out[11:])  # 25,88 kWh bei 11 kW: gut 9 Slots
    assert sum(plan_car_grid(SLOTS, prices, False, 60.0, [40.0] * 900, replace(P, car_now=True))[0]) == 0


def test_car_refills_trip_consumption_after_return():
    from custom_components.ems_mw4.planner import car_room
    # Fahrt von 11 bis 13 Uhr mit 10 kWh, Auto bei 90 %: bis zum Ladeziel fehlen 6,47 kWh, nach der Rückkehr 10 kWh mehr
    trip = {"summary": "Termin", "address": "A", "km": 50.0, "fahrzeit_min": 30, "kwh": 10.0, "kette": False,
            "abfahrt": SLOTS[44], "rueckkehr": SLOTS[52], "dep_index": 44, "back_index": 53}
    flat = [40.0] * N
    pv = [6.0 if 9 <= s.hour < 16 else 0.0 for s in SLOTS]
    plan = build_plan(SLOTS, flat, [False] * N, pv, [0.4] * N, [0.0] * N, 70.0, 55.0, True, 90.0, flat, P, trips=[trip])
    car = plan["car_kw"]
    assert plan["car"]["bedarf_kwh"] == 16.5 and plan["car"]["fahrten_kwh"] == 10.0
    assert abs(sum(car[:44]) * SLOT_H - 6.47) < 0.01  # vor der Abfahrt nur bis zum Ladeziel
    assert all(v == 0 for v in car[44:53])  # unterwegs kein Laden
    assert abs(sum(car[53:]) * SLOT_H - 10.0) < 0.01  # danach der Verbrauch der Fahrt
    # ohne Fahrt bleibt es beim Bedarf bis zum Ladeziel
    plan = build_plan(SLOTS, flat, [False] * N, pv, [0.4] * N, [0.0] * N, 70.0, 55.0, True, 90.0, flat, P)
    assert plan["car"]["bedarf_kwh"] == 6.5 and abs(sum(plan["car_kw"]) * SLOT_H - 6.47) < 0.01
    # sehr günstige Fenster vor und nach der Fahrt: vorher höchstens der freie Platz, der Rest danach
    prices = [40.0] * N
    for i in list(range(8, 12)) + list(range(100, 104)):
        prices[i] = 20.0
    hist = [40.0] * 900 + [20.0] * 60
    away = [44 <= i < 53 for i in range(N)]
    out, info = plan_car_grid(SLOTS, prices, True, 90.0, hist, P, 0.0, car_room(N, 90.0, [trip], P), away)
    assert abs(sum(out[:44]) * SLOT_H - 6.47) < 0.01 and abs(sum(out[53:]) * SLOT_H - 10.0) < 0.01
    assert all(v == 0 for i, v in enumerate(out) if prices[i] > 20.0)
    # eine Fahrt, die erst nach dem Planfenster endet, schafft im Fenster keinen Platz
    late = dict(trip, dep_index=180, back_index=N)
    assert car_room(N, 90.0, [late], P)[-1] == car_room(N, 90.0, [], P)[-1]
    # „jetzt voll laden" füllt nur bis zum Ladeziel
    out, info = plan_car_grid(SLOTS, flat, True, 90.0, hist, replace(P, car_now=True), 0.0, car_room(N, 90.0, [trip], P), away)
    assert abs(sum(out) * SLOT_H - 6.47) < 0.01 and info["bedarf_kwh"] == 16.5


def test_car_pv_during_absence_does_not_count():
    # PV nur am ersten Tag, das Auto ist dann unterwegs: der Überschuss deckt den Bedarf nicht, Netzladen im günstigen Fenster
    trip = {"summary": "Termin", "address": "A", "km": 50.0, "fahrzeit_min": 30, "kwh": 10.0, "kette": False,
            "abfahrt": SLOTS[32], "rueckkehr": SLOTS[65], "dep_index": 32, "back_index": 66}
    prices = [40.0] * N
    for i in range(100, 108):
        prices[i] = 20.0
    hist = [40.0] * 900 + [20.0] * 100
    pv = [6.0 if 36 <= i < 64 else 0.0 for i in range(N)]
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 60.0, 55.0, True, 80.0, hist, P, trips=[trip])
    assert plan["car"]["pv_deckt_bedarf"] is False and sum(plan["car_grid_kw"][100:108]) > 0
    # steht das Auto zu Hause, reicht derselbe Überschuss
    plan = build_plan(SLOTS, prices, [False] * N, pv, [0.4] * N, [0.0] * N, 60.0, 55.0, True, 80.0, hist, P)
    assert plan["car"]["pv_deckt_bedarf"] is True and sum(plan["car_grid_kw"]) == 0


def test_proactive_raises_days_before_cold():
    from datetime import date
    from custom_components.ems_mw4 import thermal as th
    # heute 13 Grad im Mittel, in drei Tagen 6 Grad: Raum noch warm und stabil, trotzdem schon anheben
    daily = [(date(2026, 10, 4), 13.0), (date(2026, 10, 5), 13.5), (date(2026, 10, 6), 12.0), (date(2026, 10, 7), 6.0),
             (date(2026, 10, 9), 2.0)]
    cold = th.coldest_day(daily, date(2026, 10, 4), 4)
    assert cold == 6.0  # heute zählt nicht, der Tag in 5 Tagen auch nicht
    adv = th.advise(21.5, 0.0, 13.0, [13.0] * 96, P, cold)
    assert adv["early_k"] == 1.5 and adv["shift_k"] == 1.5 and "kälter" in adv["grund"]
    # ist die Kälte da (Vergangenheit so kalt wie die Aussicht), fällt die Vorab-Anhebung weg
    assert th.advise(21.5, 0.0, 6.0, [6.0] * 96, P, 6.0)["early_k"] == 0.0
    # wird es nur milder oder bleibt über der Heizgrenze: nichts
    assert th.advise(21.5, 0.0, 13.0, [13.0] * 96, P, 16.0)["shift_k"] == 0.0
    # Raum schon deutlich zu warm: nicht noch anheben; Obergrenze gilt
    assert th.advise(23.5, 0.0, 13.0, [13.0] * 96, P, 6.0)["shift_k"] <= 0.0
    assert th.advise(20.0, -0.05, 15.0, [10.0] * 96, P, -5.0)["shift_k"] == P.heat_shift_max_k
