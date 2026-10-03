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
