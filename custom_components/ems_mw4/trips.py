"""Fahrten aus dem Kalender: Termine mit voller Adresse, Abfahrt, Rückkehr und Energiebedarf.

Reine Funktionen. Ein Termin zählt nur als Fahrt, wenn sein Ort Koordinaten oder eine
Adresse mit Postleitzahl hat und mehr als 5 km entfernt liegt.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
import re
from typing import Any

from .const import SLOT_MIN, Params

_PLZ = re.compile(r"\b\d{5}\b")


def is_address(location: str | None) -> bool:
    return bool(location and _PLZ.search(location))


_OVERNIGHT = ("#übernachtung", "#uebernachtung", "#&uuml;bernachtung")


def _day_time(day: date, hours: float, tz: Any) -> datetime:
    return datetime.combine(day, time(int(hours), int(round(hours % 1 * 60))), tzinfo=tz)


def parse_events(events: list[dict[str, Any]], now: datetime, p: Params | None = None) -> list[dict[str, Any]]:
    """Kalendereinträge zu Aufenthalten am Zielort.

    - Termin mit Uhrzeit am selben Tag: ein Aufenthalt von Beginn bis Ende.
    - Ganztägig: je Tag abwesend von 7 bis 17 Uhr (einstellbar), feste Abfahrt und Rückkehr.
    - Mehrtägig mit Uhrzeit: tägliche Hin- und Rückfahrt, erster Tag ab Beginn, letzter Tag bis Ende.
    - „#übernachtung" in den Notizen: einmal hin, einmal zurück, dazwischen durchgehend abwesend.
    Ohne Koordinaten oder Adresse mit Postleitzahl ist ein Termin keine Fahrt.
    """
    p = p or Params()
    visits: list[dict[str, Any]] = []
    for event in events:
        start, end, location = event.get("start"), event.get("end"), (event.get("location") or "").strip()
        coords = event.get("coords")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        if not coords and not is_address(location):
            continue
        base = {"summary": event.get("summary") or "", "address": location or coords, "coords": coords}
        overnight = any(tag in (event.get("description") or "").lower() for tag in _OVERNIGHT)
        try:
            if "T" not in start:  # ganztägig, Ende ist der Tag danach
                first, last = date.fromisoformat(start[:10]), date.fromisoformat(end[:10]) - timedelta(days=1)
                begin, finish, all_day = None, None, True
            else:
                begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
                first, last, all_day = begin.astimezone(now.tzinfo).date(), finish.astimezone(now.tzinfo).date(), False
        except ValueError:
            continue
        if last < first:
            last = first
        if (last - first).days > 31:
            continue
        if not all_day and (first == last or finish - begin < timedelta(hours=24)):
            visits.append({**base, "start": begin, "end": finish, "depart_fixed": None, "back_fixed": None})
            continue
        go, home = p.allday_depart_h, p.allday_back_h
        if overnight:
            visits.append({
                **base, "start": begin or _day_time(first, go, now.tzinfo), "end": finish or _day_time(last, home, now.tzinfo),
                "depart_fixed": None if begin else _day_time(first, go, now.tzinfo),
                "back_fixed": None if finish else _day_time(last, home, now.tzinfo),
            })
            continue
        day = first
        while day <= last:
            early = begin if (begin and day == first) else None
            late = finish if (finish and day == last) else None
            visits.append({
                **base, "start": early or _day_time(day, go, now.tzinfo), "end": late or _day_time(day, home, now.tzinfo),
                "depart_fixed": None if early else _day_time(day, go, now.tzinfo),
                "back_fixed": None if late else _day_time(day, home, now.tzinfo),
            })
            day += timedelta(days=1)
    return sorted((v for v in visits if v["start"] > now), key=lambda v: v["start"])


def _hop_km(a: str | None, b: str | None) -> float:
    """Geschätzte Straßenentfernung zwischen zwei Zielen: Luftlinie mal 1,3."""
    try:
        lat1, lon1 = (math.radians(float(x)) for x in (a or "").split(","))
        lat2, lon2 = (math.radians(float(x)) for x in (b or "").split(","))
    except ValueError:
        return 0.0
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h)) * 1.3


def build_trips(visits: list[dict[str, Any]], routes: dict[str, dict[str, Any]], p: Params) -> list[dict[str, Any]]:
    """Aufenthalte zu Fahrten: Abfahrt, Rückkehr, Energie. Termine mit unter 2 Stunden Abstand werden zur Kette."""
    legs = []
    for visit in visits:
        route = routes.get(visit["address"])
        if not route or route.get("km") is None or route.get("min") is None:
            continue
        if route["km"] <= p.trip_min_km or route["km"] > 600:
            continue
        travel = timedelta(minutes=route["min"])
        legs.append({
            **visit, "km_hin": route["km"], "km_rueck": route["km"], "fahrzeit_min": round(route["min"]),
            "km": round(2 * route["km"], 1),
            "abfahrt": visit["depart_fixed"] or visit["start"] - travel - timedelta(minutes=p.trip_buffer_min),
            "rueckkehr": visit["back_fixed"] or visit["end"] + travel,
        })
    legs.sort(key=lambda t: t["abfahrt"])
    out: list[dict[str, Any]] = []
    for leg in legs:
        prev = out[-1] if out else None
        if prev is not None and leg["start"] - prev["end"] < timedelta(hours=p.trip_chain_gap_h) and leg["start"] >= prev["start"]:
            same = leg["address"] == prev["address"]
            hop = 0.0 if same else _hop_km(prev["coords"], leg["coords"])
            prev["km"] = round(prev["km"] - prev["km_rueck"] + hop + leg["km_rueck"], 1)
            prev["km_rueck"] = leg["km_rueck"]
            prev["summary"] = f"{prev['summary']} + {leg['summary']}"
            prev["end"], prev["rueckkehr"] = leg["end"], max(prev["rueckkehr"], leg["rueckkehr"])
            prev["address"], prev["coords"], prev["kette"] = leg["address"], leg["coords"], True
        else:
            out.append({**leg, "kette": False})
    for trip in out:
        trip["kwh"] = round(trip["km"] * p.car_kwh_per_100km / 100.0, 1)
    return out


def with_routes(
    visits: list[dict[str, Any]], routes: dict[str, dict[str, Any]], slots: list[datetime], p: Params
) -> list[dict[str, Any]]:
    """Fahrten im Planfenster samt Slot-Indizes."""
    step = timedelta(minutes=SLOT_MIN)
    out = []
    for trip in build_trips(visits, routes, p):
        if trip["abfahrt"] < slots[0] or trip["abfahrt"] >= slots[-1] + step:
            continue
        out.append({
            **trip, "dep_index": int((trip["abfahrt"] - slots[0]) / step),
            "back_index": min(len(slots), int((trip["rueckkehr"] - slots[0]) / step) + 1),
        })
    return out


def later_trips(
    visits: list[dict[str, Any]], routes: dict[str, dict[str, Any]], slots: list[datetime], p: Params
) -> list[dict[str, Any]]:
    """Fahrten nach dem Planfenster (bis 7 Tage): nur zur Anzeige des Ladebedarfs."""
    end = slots[-1] + timedelta(minutes=SLOT_MIN)
    return [
        {"summary": t["summary"], "abfahrt": t["abfahrt"].isoformat(), "km": t["km"], "kwh": t["kwh"]}
        for t in build_trips(visits, routes, p) if t["abfahrt"] >= end
    ]


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


def location_coords(location: Any) -> str | None:
    """Koordinaten des Ortes als „Breite,Länge", wenn Microsoft sie mitliefert."""
    if not isinstance(location, dict):
        return None
    point = location.get("coordinates") or {}
    lat, lon = point.get("latitude"), point.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        return f"{lat},{lon}"
    return None


def location_shape(location: Any) -> dict[str, Any]:
    """Aufbau des Ortsobjekts ohne Inhalte, nur zur Diagnose."""
    if not isinstance(location, dict):
        return {"typ": type(location).__name__}
    address = location.get("address")
    return {
        "felder": sorted(location.keys()),
        "adresse_felder": sorted(k for k, v in address.items() if v) if isinstance(address, dict) else None,
        "koordinaten": location_coords(location) is not None,
    }


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
            "description": str(getattr(obj, "body", "") or ""),
            "coords": location_coords(getattr(obj, "location", None)),
            "shape": location_shape(getattr(obj, "location", None)),
        })
    return out
