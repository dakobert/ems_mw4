"""Ausführer: leitet aus dem Plan Befehle ab und schreibt sie nur, wenn die Steuerung aktiv ist.

Die Entscheidung (decide) ist eine reine Funktion. Im Schattenbetrieb wird sie
angezeigt, aber nicht ausgeführt.
"""

from __future__ import annotations

import struct
from typing import Any

from .const import Params


def float_words(watts: float) -> list[int]:
    """Float32 für Kostal-Register 1034: [unteres Wort, oberes Wort]. Negativ = Laden."""
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(watts)))
    return [lo, hi]


def car_setpoint(kw: float) -> tuple[str, int]:
    """Phasenmodus und Strom für eine Ladeleistung. Unter 4,2 kW einphasig."""
    if kw < 4.2:
        return "one_phase", max(6, min(16, round(kw * 1000 / 230)))
    return "three_phases", max(6, min(16, round(kw * 1000 / (3 * 230))))


def car_available_w(wallbox_w: float, grid_w: float, battery_w: float, battery_soc: float | None, p: Params) -> float:
    """Leistung, die das Auto jetzt aus PV bekommen darf (W).

    Netz: + Bezug, - Einspeisung. Speicher: + Entladen, - Laden. Entladen ins Auto zählt als Fehlbetrag.
    Ladeleistung des Speichers steht dem Auto erst ab car_after_battery_soc zur Verfügung.
    """
    avail = wallbox_w - grid_w
    if battery_w > 0:
        avail -= battery_w
    elif battery_soc is not None and battery_soc >= p.car_after_battery_soc:
        avail -= battery_w
    return avail


def car_pv_setpoint(avail_kw: float, psm_now: str | None, p: Params) -> tuple[str, int] | None:
    """Phasen und Strom für PV-Laden, abgerundet (kein Netzbezug). None = nicht laden.

    Dreiphasig ab 4,6 kW, zurück auf einphasig erst unter 4,14 kW (6 A dreiphasig).
    """
    if avail_kw < p.car_min_kw:
        return None
    three_min = 3 * 230 * 6 / 1000.0
    if avail_kw >= 4.6 or (psm_now == "three_phases" and avail_kw >= three_min):
        return "three_phases", max(6, min(16, int(avail_kw * 1000 // (3 * 230))))
    return "one_phase", max(6, min(16, int(avail_kw * 1000 // 230)))


def decide(plan: dict[str, Any] | None, i: int | None, data: dict[str, Any], plan_age_s: float | None,
           max_age_s: float, p: Params) -> dict[str, Any]:
    """Soll-Befehle für den laufenden Slot. 'grund' erklärt, warum nichts geschrieben wird."""
    out: dict[str, Any] = {"battery_w": None, "battery": "Kostal regelt", "dhw": None, "car": None, "heat": None, "heat_grund": None, "grund": None}
    if plan is None or i is None:
        out["grund"] = "kein Plan"
        return out
    if plan_age_s is None or plan_age_s > max_age_s:
        out["grund"] = "Plan veraltet"
        return out
    soc = data.get("battery_soc")
    action = plan["battery_action"][i]
    if soc is None or data.get("grid_power") is None:
        out["battery"] = "Messwert fehlt, Kostal regelt"
    elif action == "Netzladen" and soc < 100:
        watts = -round(min(-plan["battery_kw"][i], p.battery_charge_kw) * 1000)
        out["battery_w"], out["battery"] = watts, f"Netzladen {-watts} W"
    elif action == "Halten" and soc > p.battery_min_soc:
        out["battery_w"], out["battery"] = 0, "Entladen gesperrt"
    # Warmwasser: im geplanten Slot wird der Sollwert angehoben
    if data.get("dhw_temp") is not None:
        out["dhw"] = plan["dhw_kw"][i] > 0
    # Heizung: Vorheizen = Zustand 3, Sperre/Ruhe = Zustand 1. Abbruch, wenn der Raum zu kalt wird.
    mode = (plan.get("heat_mode") or ["normal"] * (i + 1))[i]
    room = data.get("room_temp")
    out["heat"] = "normal"
    if data.get("sg_ready") is not None:
        if mode in ("sperre", "ruhe"):
            if room is None:
                out["heat_grund"] = "Raumtemperatur fehlt, keine Sperre"
            elif room < p.room_target_c - p.room_band_down_c:
                out["heat_grund"] = "Raum zu kalt, Sperre abgebrochen"
            else:
                out["heat"] = mode
        elif mode == "vorheizen":
            if room is not None and room >= p.room_target_c + p.room_band_up_c:
                out["heat_grund"] = "Raum warm genug, kein Vorheizen"
            else:
                out["heat"] = "vorheizen"
    else:
        out["heat"] = None
    # Wallbox
    if data.get("car_connected") == 1.0 and data.get("wallbox_power") is not None:
        kw = plan.get("car_grid_kw", plan["car_kw"])[i]
        car_soc = data.get("car_soc")
        if kw >= p.car_min_kw:
            mode, amps = car_setpoint(kw)
            out["car"] = {"frc": "charge", "psm": mode, "amp": amps, "kw": kw}
        elif car_soc is None or car_soc < p.car_target_soc:
            out["car"] = {"frc": "pv"}  # Leistung regelt der Coordinator nach dem echten Überschuss
        else:
            out["car"] = {"frc": "dont_charge"}
    return out


def sg_state(dhw: bool | None, heat: str | None, auto_dhw: bool, auto_heat: bool) -> int | None:
    """SG-Ready-Zielzustand aus Warmwasser- und Heizungswunsch. None = nichts anfassen.

    1 = Sperre (Eingang 2 an), 2 = normal, 3 = anheben (Eingang 1 an). Zustand 4 wird nie gesetzt.
    Warmwasser läuft über die Sollwerte; im geplanten Fenster verhindert es nur eine Sperre.
    """
    want_dhw = bool(dhw) and auto_dhw
    known = (dhw is not None and auto_dhw) or (heat is not None and auto_heat)
    if not known:
        return None
    if want_dhw:
        return 2
    if auto_heat and heat in ("sperre", "ruhe"):
        return 1
    if auto_heat and heat == "vorheizen":
        return 3
    return 2


SG_INPUTS = {1: ("off", "on"), 2: ("off", "off"), 3: ("on", "off")}


def dhw_target(want: bool | None, temp: float | None, done: bool, p: Params,
               running_min: float | None = None) -> tuple[float, bool]:
    """Warmwasser-Sollwert und ob die Ladung dieses Fensters erledigt ist.

    Im geplanten Fenster gilt der Ladesollwert, bis der Speicher die Hochlade-Schwelle erreicht hat.
    Eine begonnene Ladung läuft über das Fensterende hinaus weiter, bis die Schwelle erreicht ist,
    höchstens dhw_max_min ab Beginn. Danach und außerhalb des Fensters gilt der Grundwert.
    """
    active = bool(want) or (running_min is not None and running_min < p.dhw_max_min)
    if not active:
        return p.dhw_base_c, False
    if done or (temp is not None and temp >= p.dhw_hot_c):
        return p.dhw_base_c, True
    return p.dhw_charge_c, False


def pv_dhw(data: dict[str, Any], latched_min: float | None, done_today: bool, p: Params) -> tuple[bool, bool]:
    """Warmwasser mit PV-Überschuss laden, auch wenn der Speicher über der Tagesschwelle liegt.

    Start: Hausspeicher voll und mindestens dhw_pv_export_w Einspeisung. Läuft die Ladung einmal,
    bleibt sie an (der Verdichter frisst den Überschuss selbst), bis die Hochlade-Schwelle erreicht
    oder dhw_pv_max_min vergangen ist. Höchstens einmal am Tag.
    Rückgabe: (jetzt laden, für heute erledigt).
    """
    temp = data.get("dhw_temp")
    if latched_min is not None:
        if (temp is not None and temp >= p.dhw_hot_c) or latched_min >= p.dhw_pv_max_min:
            return False, True
        return True, False
    if done_today or temp is None or temp >= p.dhw_hot_c:
        return False, done_today
    soc, grid = data.get("battery_soc"), data.get("grid_power")
    if soc is not None and grid is not None and soc >= p.dhw_pv_soc and -grid >= p.dhw_pv_export_w:
        return True, False
    return False, False
