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
    p: Params, cold_ahead: float | None = None,
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
    # Kälte in den nächsten Tagen: schon jetzt anheben, der Estrich braucht ein bis zwei Tage
    reference = out_past if out_past is not None else out_ahead
    early = 0.0
    if cold_ahead is not None and reference is not None and cold_ahead < p.heat_limit_c:
        early = _half_steps(max(0.0, reference - cold_ahead) * p.heat_days_gain)
    out["cold_ahead_c"] = None if cold_ahead is None else round(cold_ahead, 1)
    out["early_k"] = early
    if early <= 0 and out_ahead is not None and out_ahead >= p.heat_limit_c and predicted >= p.room_target_c:
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
    if early > 0 and predicted <= p.room_target_c + p.room_band_up_c:
        shift = min(p.heat_shift_max_k, max(shift, 0.0) + early)
        out["grund"] = "es wird in den nächsten Tagen kälter, früh anheben"
    out["shift_k"] = shift
    return out


def coldest_day(daily: list[tuple[Any, float]], today: Any, days: float) -> float | None:
    """Kälteste Tagesmitteltemperatur der kommenden Tage (ohne heute)."""
    values = [mean for day, mean in daily if 0 < (day - today).days <= days]
    return min(values) if values else None


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


def fit_house(samples: list[dict[str, Any]], window: int = 24) -> dict[str, Any]:
    """Wärmeverhalten des Hauses aus der Messreihe lernen.

    Je 6-Stunden-Fenster: Änderung der Raumreferenz (K/h) erklärt durch
      - den Abstand zur Außentemperatur (Auskühlen),
      - die Leistung der Wärmepumpe (Aufheizen),
      - die PV-Leistung als Maß für die Sonne,
      - einen festen Rest (Bewohner, Geräte).
    Das Ergebnis gilt erst mit genug Fenstern und plausiblen Werten als belastbar.
    """
    import numpy as np

    rows: list[list[float]] = []
    change: list[float] = []
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start : start + window]
        if any(s.get("room_temp") is None or s.get("outdoor_temp") is None for s in chunk):
            continue
        head = sum(s["room_temp"] for s in chunk[:4]) / 4
        tail = sum(s["room_temp"] for s in chunk[-4:]) / 4
        hours = (window - 4) * 0.25
        gap = sum(s["room_temp"] - s["outdoor_temp"] for s in chunk) / window
        heat = sum((s.get("hp_power") or 0.0) for s in chunk) / window / 1000.0
        sun = sum((s.get("pv_power") or 0.0) for s in chunk) / window / 1000.0
        rows.append([-gap, heat, sun, 1.0])
        change.append((tail - head) / hours)
    out: dict[str, Any] = {"fenster": len(rows), "gueltig": False}
    if len(rows) < 12:
        return out
    coef, *_ = np.linalg.lstsq(np.array(rows), np.array(change), rcond=None)
    loss, heat_gain, sun_gain, rest = (float(v) for v in coef)
    out.update({
        "auskuehlen_k_je_h_je_k": round(loss, 5),
        "heizen_k_je_h_je_kw": round(heat_gain, 4),
        "sonne_k_je_h_je_kw_pv": round(sun_gain, 4),
        "rest_k_je_h": round(rest, 4),
        "zeitkonstante_h": round(1.0 / loss, 0) if loss > 0 else None,
    })
    # belastbar: mindestens 10 Tage und eine Zeitkonstante zwischen 20 und 400 Stunden
    out["gueltig"] = len(rows) >= 40 and 0.0025 <= loss <= 0.05 and heat_gain >= 0
    return out


def learned_coupling(model: dict[str, Any], hours: float, default: float) -> float:
    """Wie stark die Raumtemperatur in `hours` Stunden einer Änderung der Außentemperatur folgt (K je K)."""
    import math

    if not model.get("gueltig"):
        return default
    return round(1.0 - math.exp(-model["auskuehlen_k_je_h_je_k"] * hours), 3)
