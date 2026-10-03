"""Fahrten aus dem Kalender: Termine mit voller Adresse, Abfahrt, Rückkehr und Energiebedarf.

Reine Funktionen. Ein Termin zählt nur als Fahrt, wenn im Ortsfeld eine Adresse mit
Postleitzahl steht. Ganztägige Termine haben keine Abfahrtszeit und zählen nicht.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from .const import SLOT_MIN, Params

_PLZ = re.compile(r"\b\d{5}\b")


def is_address(location: str | None) -> bool:
    return bool(location and _PLZ.search(location))


def parse_events(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Kalendereinträge zu Fahrten: nur Termine mit Uhrzeit, Adresse und Beginn in der Zukunft."""
    trips = []
    for event in events:
        start, end, location = event.get("start"), event.get("end"), (event.get("location") or "").strip()
        if not isinstance(start, str) or "T" not in start or not isinstance(end, str) or not is_address(location):
            continue
        try:
            begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError:
            continue
        if begin <= now:
            continue
        trips.append({"start": begin, "end": finish, "summary": event.get("summary") or "", "address": location})
    return sorted(trips, key=lambda t: t["start"])


def with_routes(
    trips: list[dict[str, Any]], routes: dict[str, dict[str, Any]], slots: list[datetime], p: Params
) -> list[dict[str, Any]]:
    """Abfahrt, Rückkehr, Energie und Slot-Indizes je Fahrt. Fahrten ohne bekannte Strecke bleiben draußen."""
    out = []
    step = timedelta(minutes=SLOT_MIN)
    for trip in trips:
        route = routes.get(trip["address"])
        if not route or route.get("km") is None or route.get("min") is None:
            continue
        travel = timedelta(minutes=route["min"])
        depart = trip["start"] - travel - timedelta(minutes=p.trip_buffer_min)
        back = trip["end"] + travel
        if depart < slots[0] or depart >= slots[-1] + step:
            continue
        dep_idx = int((depart - slots[0]) / step)
        back_idx = min(len(slots), int((back - slots[0]) / step) + 1)
        out.append({
            **trip, "km": route["km"], "fahrzeit_min": round(route["min"]),
            "abfahrt": depart, "rueckkehr": back, "dep_index": dep_idx, "back_index": back_idx,
            "kwh": round(2 * route["km"] * p.car_kwh_per_100km / 100.0, 1),
        })
    return out


def learn_consumption(
    samples: list[dict[str, Any]], car_kwh: float, default: float, min_km: float = 100.0, window_km: float = 500.0
) -> tuple[float, float]:
    """Verbrauch in kWh/100 km aus Kilometerstand und Ladestand der Messreihe, dazu die Kilometerbasis.

    Gezählt werden nur Abschnitte, in denen der Kilometerstand steigt und der Ladestand fällt.
    Abschnitte mit Laden unterwegs fallen heraus. Unter `min_km` gilt der eingestellte Wert.
    """
    km = kwh = 0.0
    last: tuple[float, float] | None = None
    segments: list[tuple[float, float]] = []
    for s in samples:
        odo, soc = s.get("car_mileage"), s.get("car_soc")
        if odo is None or soc is None:
            continue
        if last is not None:
            d_km, d_soc = odo - last[0], last[1] - soc
            if 0 < d_km < 600 and d_soc > 0:
                segments.append((d_km, d_soc / 100.0 * car_kwh))
        last = (odo, soc)
    for d_km, d_kwh in reversed(segments):
        if km >= window_km:
            break
        km, kwh = km + d_km, kwh + d_kwh
    if km < min_km:
        return default, round(km, 0)
    return round(max(12.0, min(35.0, kwh / km * 100.0)), 1), round(km, 0)


def location_text(location: Any) -> str:
    """Adresse aus dem Ortsobjekt von Microsoft: Straße, Postleitzahl und Ort, sonst der Anzeigename."""
    if not isinstance(location, dict):
        return str(location or "")
    address = location.get("address") or {}
    street, code, city = (str(address.get(k) or "").strip() for k in ("street", "postalCode", "city"))
    if street and code and city:
        return f"{street}, {code} {city}"
    return str(location.get("displayName") or "").strip()


def events_from_objects(objects: Any) -> list[dict[str, Any]]:
    """Terminobjekte der Kalender-Integration in das einfache Format von parse_events übersetzen."""
    out = []
    for obj in objects or []:
        start, end = getattr(obj, "start", None), getattr(obj, "end", None)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        all_day = bool(getattr(obj, "is_all_day", False))
        out.append({
            "start": start.date().isoformat() if all_day else start.isoformat(),
            "end": end.date().isoformat() if all_day else end.isoformat(),
            "summary": getattr(obj, "subject", "") or "",
            "location": location_text(getattr(obj, "location", None)),
        })
    return out
