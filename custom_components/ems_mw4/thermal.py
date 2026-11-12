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
    if early > 0 and predicted <= p.room_target_c + p.heat_early_room_margin_c:
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


def curve_gain(slope: float | None, default: float) -> float:
    """Anhebung der Komforttemperatur, die einem Grad kälterer Außentemperatur entspricht.

    An der Stiebel gemessen (04.10.2026): Vorlauf-Soll = Komfort + 1,33 * Steilheit * (Komfort - Außen).
    Ein Grad kälter draußen hebt den Vorlauf um 1,33 * Steilheit, ein Grad Komfort um 1 + 1,33 * Steilheit.
    Das Verhältnis ist die Anhebung, mit der die Heizung heute so läuft, als wäre die Kälte schon da.
    """
    if slope is None or slope <= 0:
        return default
    k = 1.33 * slope
    return round(k / (1.0 + k), 3)


def last_dhw_hot(samples: list[dict[str, Any]], threshold: float) -> datetime | None:
    """Zeitpunkt der letzten Messung, bei der der Warmwasserspeicher die Schwelle erreicht hatte."""
    for s in reversed(samples):
        value = s.get("dhw_temp")
        if value is not None and value >= threshold:
            try:
                return datetime.fromisoformat(s["t"])
            except (KeyError, ValueError):
                continue
    return None


def room_reference(values: list[float], leads: list[tuple[float, float]]) -> float | None:
    """Raumreferenz: Median der Führungsräume. Ausreißer (Kochen, Lüften) fallen heraus.

    leads: (Wert, Mindestwert) der Mindesträume (Wohnzimmer, Bad OG). Liegt einer darunter,
    zählt der kälteste davon, sofern er unter dem Median liegt.
    """
    pool = values or [v for v, _ in leads]
    if not pool:
        return None
    v = sorted(pool)
    n = len(v)
    ref = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    for value, minimum in leads:
        if value < minimum:
            ref = min(ref, value)
    return round(ref, 2)


def sunny_tomorrow(slots: list, pv_kw: list[float], load_kw: list[float], tomorrow, battery_kwh: float) -> bool:
    """Morgen bleibt nach der Hauslast mindestens ein voller Speicher PV übrig (Sommer, kaum Heizung)."""
    pv = sum(v for s, v in zip(slots, pv_kw) if s.date() == tomorrow) * 0.25
    load = sum(v for s, v in zip(slots, load_kw) if s.date() == tomorrow) * 0.25
    return pv > 0 and pv - load >= battery_kwh


# ---------- Gelerntes Trägheitsmodell (Roy 12.11.2026) ----------
# Ein-Speicher-Modell je Stunde: T[t+1] = T[t] + a·(Komfort[t−L] − T[t]) + b·(Außen[t] − T[t]) + g
#   a: wie schnell der Raum der Komforttemperatur folgt, b: Auskühlen nach außen, g: Rest (Sonne, Geräte),
#   L: Totzeit in Stunden. Gelernt wird über den Fehler echter 24-h-Vorhersagen, nicht über Stundenschritte –
#   die Fußbodenheizung ist träge, deshalb zählt, wo der Raum nach einem Tag steht.
# Messung 03.–12.11.2026 (Wohnzimmer): a 0,025, b 0,009, g 0,1, L 0 → Zeitkonstante ~29 h, 24-h-Fehler 0,13 K.
# Für 21 °C braucht es Komfort ≈ 21 bei 10 °C außen, ≈ 23 bei 4 °C, ≈ 24,5 bei 0 °C.

RESP_H = 24  # Vorhersagehorizont für das Lernen
RESP_MAX_DAYS = 30
RESP_MIN_WINDOWS = 40  # ~5 Tage zusammenhängender Daten
RESP_MAX_ERR_K = 0.35  # schlechter: Modell nicht verwenden
# 05.11. 16 Uhr bis 06.11. 11 Uhr (MEZ) galt der kälteste Raum als Referenz – nicht vergleichbar
RESP_SKIP = [(datetime.fromisoformat("2026-11-05T15:00:00+00:00"), datetime.fromisoformat("2026-11-06T10:00:00+00:00"))]


def hourly_series(samples: list[dict[str, Any]], now: datetime, days: float = RESP_MAX_DAYS) -> list[dict[str, Any]]:
    """15-Minuten-Messpunkte zu Stundenmitteln (Raum, Außen, Komfort). Stunden mit Lücken oder Abwesenheit fallen weg."""
    start = now - timedelta(days=days)
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for s in samples:
        try:
            t = datetime.fromisoformat(s["t"])
        except (KeyError, ValueError):
            continue
        if t < start or any(a <= t < b for a, b in RESP_SKIP):
            continue
        buckets.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(s)
    out = []
    for hour in sorted(buckets):
        chunk = buckets[hour]
        if len(chunk) < 3 or any(s.get("away") for s in chunk):
            continue
        vals = {}
        for key, name in (("room_temp", "room"), ("outdoor_temp", "out"), ("heat_comfort", "comfort")):
            v = [float(s[key]) for s in chunk if s.get(key) is not None]
            if len(v) < 3:
                break
            vals[name] = sum(v) / len(v)
        else:
            out.append({"t": hour, **vals})
    return out


def fit_response(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Trägheitsmodell aus Stundenwerten lernen (Gittersuche über den Fehler der 24-h-Vorhersage)."""
    import numpy as np

    res: dict[str, Any] = {"gueltig": False, "fenster": 0}
    if len(series) < RESP_H * 3:
        return res
    # zusammenhängende Stunden indizieren
    t0 = series[0]["t"]
    n = int((series[-1]["t"] - t0).total_seconds() // 3600) + 1
    room = np.full(n, np.nan)
    out = np.full(n, np.nan)
    com = np.full(n, np.nan)
    for row in series:
        i = int((row["t"] - t0).total_seconds() // 3600)
        room[i], out[i], com[i] = row["room"], row["out"], row["comfort"]
    best = None
    for lag in range(0, 7):
        starts = [s for s in range(lag, n - RESP_H, 3)
                  if not np.isnan(room[s:s + RESP_H + 1]).any() and not np.isnan(out[s:s + RESP_H]).any()
                  and not np.isnan(com[s - lag:s - lag + RESP_H]).any()]
        if len(starts) < RESP_MIN_WINDOWS:
            continue
        st = np.array(starts)
        target = room[st + RESP_H]
        for a in np.linspace(0.005, 0.08, 16):
            for b in np.linspace(0.0, 0.03, 11):
                for g in np.linspace(-0.1, 0.2, 16):
                    x = room[st].copy()
                    for k in range(RESP_H):
                        x = x + a * (com[st + k - lag] - x) + b * (out[st + k] - x) + g
                    err = float(np.abs(x - target).mean())
                    if best is None or err < best[0]:
                        best = (err, float(a), float(b), float(g), lag, len(starts))
    if best is None:
        return res
    err, a, b, g, lag, windows = best
    res.update({
        "a": round(a, 4), "b": round(b, 4), "g": round(g, 4), "verzoegerung_h": lag, "fenster": windows,
        "fehler_24h_k": round(err, 3), "zeitkonstante_h": round(1.0 / (a + b), 1) if a + b > 0 else None,
    })
    res["gueltig"] = a >= 0.008 and err <= RESP_MAX_ERR_K
    return res


def comfort_for(model: dict[str, Any], room_target: float, outdoor: float) -> float | None:
    """Komforttemperatur, bei der sich der Raum auf Dauer bei room_target einpendelt."""
    if not model.get("gueltig"):
        return None
    a, b, g = model["a"], model["b"], model["g"]
    return round(((a + b) * room_target - b * outdoor - g) / a, 2)


def plan_comfort(
    model: dict[str, Any], room_now: float, comfort_past: list[float], out_ahead: list[float],
    p: Params, c_min: float, c_max: float, horizon_h: int = 36,
) -> dict[str, Any] | None:
    """Komforttemperatur so wählen, dass der Raum über die nächsten Stunden möglichst am Ziel liegt.

    Simuliert jede Stufe (0,5 K) mit der Wetterprognose. Bestraft wird die Abweichung vom Ziel und besonders
    jedes Überschreiten von Ziel + Band: so wird früh zurückgenommen, bevor der Estrich überheizt, und früh
    angehoben, bevor eine kalte Nacht kommt.
    """
    if not model.get("gueltig") or room_now is None or not out_ahead:
        return None
    a, b, g, lag = model["a"], model["b"], model["g"], int(model["verzoegerung_h"])
    target, upper = p.room_target_c, p.room_target_c + p.room_band_up_c
    outs = (list(out_ahead) + [out_ahead[-1]] * horizon_h)[:horizon_h]
    past = list(comfort_past)[-lag:] if lag else []
    best = None
    c = c_min
    while c <= c_max + 1e-9:
        x, traj = room_now, []
        for k in range(horizon_h):
            ck = past[k] if k < len(past) else c
            x = x + a * (ck - x) + b * (outs[k] - x) + g
            traj.append(x)
        cost = 0.0
        for k, v in enumerate(traj):
            w = 1.0 if k >= 6 else 0.3  # die ersten Stunden sind ohnehin kaum zu ändern
            cost += w * (v - target) ** 2 + 10.0 * max(0.0, v - upper) ** 2
        cost += 0.001 * (c - c_min)  # bei Gleichstand die sparsamere Stufe
        if best is None or cost < best[0]:
            best = (cost, c, traj)
        c = round(c + 0.5, 1)
    _, c, traj = best
    return {"comfort_c": c, "raum_24h_c": round(traj[min(23, len(traj) - 1)], 2),
            "raum_max_c": round(max(traj), 2), "raum_min_c": round(min(traj), 2)}
