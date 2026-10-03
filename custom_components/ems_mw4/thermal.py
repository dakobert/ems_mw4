"""Vorausschauendes Heizen für die träge Fußbodenheizung.

Reine Funktionen. Aus dem Verlauf der Raumreferenz und der Außentemperatur-Prognose
wird geschätzt, wo die Raumtemperatur in `lookahead_h` Stunden läge, wenn nichts
geändert wird. Daraus folgt eine Parallelverschiebung der Heizkurve (Komforttemperatur)
und, bei dauerhaftem Bedarf, eine langsame Anpassung der Steilheit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .const import Params


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def room_slope(samples: list[dict[str, Any]], now: datetime, hours: float, key: str = "room_temp") -> float | None:
    """Steigung der Raumtemperatur in K/h (kleinste Quadrate) über die letzten `hours` Stunden."""
    start = now - timedelta(hours=hours)
    points: list[tuple[float, float]] = []
    for s in samples:
        value = s.get(key)
        if value is None:
            continue
        try:
            t = datetime.fromisoformat(s["t"])
        except (KeyError, ValueError):
            continue
        if t >= start:
            points.append(((t - start).total_seconds() / 3600.0, float(value)))
    if len(points) < 12 or points[-1][0] - points[0][0] < hours * 0.5:
        return None
    mx = sum(x for x, _ in points) / len(points)
    my = sum(y for _, y in points) / len(points)
    den = sum((x - mx) ** 2 for x, _ in points)
    if den <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / den


def outdoor_past_mean(samples: list[dict[str, Any]], now: datetime, hours: float) -> float | None:
    start = now - timedelta(hours=hours)
    values = []
    for s in samples:
        value = s.get("outdoor_temp")
        if value is None:
            continue
        try:
            if datetime.fromisoformat(s["t"]) >= start:
                values.append(float(value))
        except (KeyError, ValueError):
            continue
    return _mean(values) if len(values) >= 12 else None


def _half_steps(value: float) -> float:
    return round(value * 2) / 2


def advise(
    room: float | None, slope_k_h: float | None, out_past: float | None, temps_ahead: list[float | None],
    p: Params,
) -> dict[str, Any]:
    """Empfohlene Verschiebung der Komforttemperatur (K, in 0,5er-Schritten) und die Begründung."""
    out: dict[str, Any] = {"shift_k": 0.0, "predicted_c": None, "grund": None, "slope_k_h": slope_k_h,
                           "out_past_c": out_past, "out_ahead_c": None}
    if room is None:
        out["grund"] = "Raumtemperatur fehlt"
        return out
    ahead = [t for t in temps_ahead if t is not None]
    out_ahead = _mean(ahead)
    out["out_ahead_c"] = None if out_ahead is None else round(out_ahead, 1)
    predicted = room
    # Trend der letzten Stunden fortschreiben, gedeckelt gegen Ausreißer (Lüften, Sonne)
    if slope_k_h is not None:
        predicted += max(-0.1, min(0.1, slope_k_h)) * p.heat_lookahead_h
    # Wetteränderung: wird es draußen kälter als zuletzt, kühlt das Haus zusätzlich aus
    if out_ahead is not None and out_past is not None:
        predicted += p.heat_outdoor_coupling * (out_ahead - out_past)
    out["predicted_c"] = round(predicted, 2)
    if out_ahead is not None and out_ahead >= p.heat_limit_c and predicted >= p.room_target_c:
        out["grund"] = "über Heizgrenze, kein Bedarf"
        return out
    deficit = p.room_target_c - predicted
    if deficit > 0:
        # lieber zu früh als zu spät: schon kleine Unterschreitungen zählen voll
        shift = min(p.heat_shift_max_k, _half_steps(deficit * p.heat_shift_gain + 0.25))
        out["grund"] = "Raum wird voraussichtlich zu kühl, früh anheben"
    elif -deficit > p.room_band_up_c:
        shift = max(-p.heat_shift_min_k, -_half_steps((-deficit - p.room_band_up_c) * p.heat_shift_gain))
        out["grund"] = "Raum wird voraussichtlich zu warm, absenken"
    else:
        shift = 0.0
        out["grund"] = "im Band"
    out["shift_k"] = shift
    return out


def curve_target(current: float | None, shift_history: list[float], p: Params) -> float | None:
    """Steilheit langsam nachführen: bleibt die Verschiebung tagelang einseitig, ändert sich die Kurve um 0,05."""
    if current is None or len(shift_history) < 3 * 96 * 0.8:
        return None
    mean = sum(shift_history) / len(shift_history)
    if mean >= 1.0 and current < p.heat_curve_max:
        target = min(p.heat_curve_max, current + 0.05)
    elif mean <= -0.5 and current > p.heat_curve_min:
        target = max(p.heat_curve_min, current - 0.05)
    else:
        return None
    target = round(target, 2)
    return target if abs(target - current) >= 0.01 else None
