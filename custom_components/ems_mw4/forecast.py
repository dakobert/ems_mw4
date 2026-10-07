"""Prognose-Bausteine. Reine Funktionen ohne Home-Assistant-Abhängigkeit."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable

from .const import HORIZON_SLOTS, SLOT_MIN


def slot_start(now: datetime) -> datetime:
    """Beginn des laufenden 15-Minuten-Fensters."""
    return now.replace(minute=now.minute - now.minute % SLOT_MIN, second=0, microsecond=0)


def build_slots(now: datetime, count: int = HORIZON_SLOTS) -> list[datetime]:
    start = slot_start(now)
    return [start + timedelta(minutes=SLOT_MIN * i) for i in range(count)]


def price_series(slots: list[datetime], known: dict[datetime, float]) -> tuple[list[float | None], list[bool]]:
    """Preis je Slot in ct/kWh. Unbekannte Slots: gleicher Zeitpunkt am letzten bekannten Tag.

    Rückgabe: (Preise, geschätzt-Kennzeichen).
    """
    prices: list[float | None] = []
    estimated: list[bool] = []
    for slot in slots:
        if slot in known:
            prices.append(known[slot])
            estimated.append(False)
            continue
        value = None
        for days in range(1, 8):
            earlier = slot - timedelta(days=days)
            if earlier in known:
                value = known[earlier]
                break
        prices.append(value)
        estimated.append(True)
    return prices, estimated


def pv_series(slots: list[datetime], hours_by_date: dict[Any, dict[str, float]]) -> list[float]:
    """PV-Leistung je Slot in kW aus Stundenwerten (kWh je Stunde), örtliche Zeit."""
    out: list[float] = []
    for slot in slots:
        day = hours_by_date.get(slot.date())
        value = 0.0
        if day:
            raw = day.get(f"{slot.hour:02d}:00")
            try:
                value = max(0.0, float(raw)) if raw is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
        out.append(value)  # kWh in einer Stunde = mittlere kW
    return out


def base_load_profile(rows: Iterable[tuple[datetime, float]]) -> dict[tuple[bool, int], float]:
    """Grundlast-Profil in W: Median je (Wochenende, Stunde) aus Stundenmitteln."""
    buckets: dict[tuple[bool, int], list[float]] = defaultdict(list)
    for when, watts in rows:
        if watts is None or watts != watts:
            continue
        buckets[(when.weekday() >= 5, when.hour)].append(max(0.0, watts))
    return {key: median(values) for key, values in buckets.items() if len(values) >= 3}


def base_load_series(slots: list[datetime], profile: dict[tuple[bool, int], float], default_w: float) -> list[float]:
    """Grundlast je Slot in kW."""
    out: list[float] = []
    for slot in slots:
        watts = profile.get((slot.weekday() >= 5, slot.hour))
        if watts is None:
            watts = profile.get((slot.weekday() < 5, slot.hour), default_w)
        out.append(max(50.0, watts) / 1000.0)
    return out


def fit_heat(days: Iterable[tuple[float, float]], limit_c: float, default_w_per_k: float) -> tuple[float, int]:
    """Elektrische Heizleistung je Kelvin unter der Heizgrenze (W/K) aus Tagesmitteln.

    days: (mittlere Außentemperatur °C, mittlere Wärmepumpenleistung W).
    Gerade durch den Ursprung. Zu wenig Daten: Vorgabewert.
    """
    num = den = 0.0
    count = 0
    for temp, watts in days:
        if temp is None or watts is None or temp >= limit_c - 2.0:
            continue
        delta = limit_c - temp
        num += delta * watts
        den += delta * delta
        count += 1
    if count < 7 or den <= 0:
        return default_w_per_k, count
    return min(250.0, max(10.0, num / den)), count


def temp_series(slots: list[datetime], hourly: dict[datetime, float], fallback: float | None) -> list[float | None]:
    """Außentemperatur je Slot aus stündlicher Prognose (Schlüssel: Stundenbeginn, UTC-bewusst)."""
    out: list[float | None] = []
    last = fallback
    for slot in slots:
        key = slot.replace(minute=0)
        if key in hourly:
            last = hourly[key]
        out.append(last)
    return out


def heat_series(temps: list[float | None], limit_c: float, w_per_k: float) -> list[float]:
    """Elektrische Heizleistung je Slot in kW."""
    return [0.0 if t is None else max(0.0, limit_c - t) * w_per_k / 1000.0 for t in temps]


def quantile(values: list[float], q: float) -> float | None:
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    index = min(len(data) - 1, max(0, int(q * (len(data) - 1) + 0.5)))
    return data[index]
