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
    # Warmwasser: SG Ready Zustand 3 im geplanten Slot
    if data.get("dhw_temp") is not None and data.get("sg_ready") is not None:
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
        kw = plan["car_kw"][i]
        if kw >= p.car_min_kw:
            mode, amps = car_setpoint(kw)
            out["car"] = {"frc": "charge", "psm": mode, "amp": amps}
        else:
            out["car"] = {"frc": "dont_charge"}
    return out


def sg_state(dhw: bool | None, heat: str | None, auto_dhw: bool, auto_heat: bool) -> int | None:
    """SG-Ready-Zielzustand aus Warmwasser- und Heizungswunsch. None = nichts anfassen.

    1 = Sperre (Eingang 2 an), 2 = normal, 3 = anheben (Eingang 1 an). Zustand 4 wird nie gesetzt.
    Warmwasser im geplanten Fenster geht vor einer Sperre.
    """
    want_dhw = bool(dhw) and auto_dhw
    known = (dhw is not None and auto_dhw) or (heat is not None and auto_heat)
    if not known:
        return None
    if want_dhw:
        return 3
    if auto_heat and heat in ("sperre", "ruhe"):
        return 1
    if auto_heat and heat == "vorheizen":
        return 3
    return 2


SG_INPUTS = {1: ("off", "on"), 2: ("off", "off"), 3: ("on", "off")}
