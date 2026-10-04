"""Plangenauigkeit: Prognosen festhalten, Istwerte je Stunde bilden, Fehlermaß über 7 Tage.

Reine Funktionen ohne Home-Assistant-Abhängigkeit. Wertet nur aus, steuert nichts.
Stunden werden über ihren Beginn in UTC geführt, damit Zeitumstellungen nichts verschieben.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

LEAD_TARGET_H = 12.0  # maßgeblich ist die Prognose rund 12 Stunden vor der Stunde
LEAD_MIN_H = 6.0  # ersatzweise die älteste, die mindestens 6 Stunden vorher erstellt wurde
WINDOW_DAYS = 7
KEEP_DAYS = 8
MIN_HOURS = 72
MIN_COVER_S = 54 * 60  # eine Stunde zählt nur, wenn mindestens 54 Minuten gemessen wurden
MAX_GAP_S = 150  # längere Lücken (Neustart) werden nicht geschätzt


def new_state() -> dict[str, Any]:
    return {"fc": {}, "ist": {}, "acc": None, "kosten_fc": {}, "letzte": None}


def clean_state(raw: Any) -> dict[str, Any]:
    """Gespeicherten Stand übernehmen, Unbrauchbares verwerfen."""
    state = new_state()
    if not isinstance(raw, dict):
        return state
    for key in ("fc", "ist", "kosten_fc"):
        if isinstance(raw.get(key), dict):
            state[key] = raw[key]
    if isinstance(raw.get("acc"), dict) and {"h", "last", "s", "pv", "home", "hp", "wb"} <= set(raw["acc"]):
        state["acc"] = raw["acc"]
    if isinstance(raw.get("letzte"), str):
        state["letzte"] = raw["letzte"]
    return state


def hour_start(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def hour_key(moment: datetime) -> str:
    return hour_start(moment).isoformat()


def record_forecast(
    state: dict[str, Any], created: datetime, slots: list[datetime], pv_kw: list[float], base_kw: list[float],
    slot_h: float,
) -> int:
    """Prognose je kommender voller Stunde festhalten (kWh). Rückgabe: Zahl der geschriebenen Stunden.

    Je Stunde bleibt genau eine Prognose stehen: Solange der Vorlauf 12 Stunden oder mehr beträgt,
    überschreibt die neuere die ältere, am Ende steht die von rund 12 Stunden vorher. Zwischen 6 und
    12 Stunden Vorlauf wird nur geschrieben, wenn noch keine vorhanden ist (die älteste zählt).
    """
    per_hour = round(1.0 / slot_h)
    groups: dict[str, list[int]] = {}
    for i, slot in enumerate(slots):
        groups.setdefault(hour_key(slot), []).append(i)
    written = 0
    for key, idx in groups.items():
        if len(idx) != per_hour:
            continue  # angebrochene Stunde oder Zeitumstellung
        lead_h = (datetime.fromisoformat(key) - created).total_seconds() / 3600.0
        if lead_h < LEAD_MIN_H:
            continue
        if lead_h < LEAD_TARGET_H and key in state["fc"]:
            continue
        state["fc"][key] = {
            "t": created.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "pv": round(sum(pv_kw[i] for i in idx) * slot_h, 4),
            "base": round(sum(base_kw[i] for i in idx) * slot_h, 4),
        }
        written += 1
    return written


def record_cost_forecast(state: dict[str, Any], created_local: datetime, cost_by_day: dict[str, float]) -> None:
    """Geplante Netzbezugskosten für morgen festhalten: der letzte Plan vor 12 Uhr, sonst der erste danach."""
    day = (created_local.date() + timedelta(days=1)).isoformat()
    if day not in cost_by_day:
        return
    if day not in state["kosten_fc"] or created_local.hour < 12:
        state["kosten_fc"][day] = round(float(cost_by_day[day]), 2)


def _new_acc(key: str, now: datetime) -> dict[str, Any]:
    return {"h": key, "last": now.astimezone(timezone.utc).isoformat(), "s": 0.0,
            "pv": 0.0, "home": 0.0, "hp": 0.0, "wb": 0.0}


def _finalize(state: dict[str, Any], acc: dict[str, Any], now: datetime) -> None:
    if acc["s"] < MIN_COVER_S:
        return
    scale = 3600.0 / acc["s"] / 3_600_000.0  # Ws der gemessenen Zeit -> kWh der vollen Stunde
    state["ist"][acc["h"]] = {
        "pv": round(acc["pv"] * scale, 4),
        "base": round(max(0.0, acc["home"] - acc["hp"] - acc["wb"]) * scale, 4),
    }
    if acc["h"] in state["fc"]:
        state["letzte"] = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def track(
    state: dict[str, Any], now: datetime, pv_w: float | None, home_w: float | None,
    hp_w: float | None, wb_w: float | None,
) -> bool:
    """Messwerte der letzten Abfrage aufsummieren. True, wenn eine Stunde abgeschlossen wurde.

    Grundlast-Ist = Hausverbrauch minus Wärmepumpe minus Wallbox, wie im Grundlastprofil.
    Fehlt ein Wert oder ist die Lücke zu groß, zählt die Zeit nicht als gemessen.
    """
    key = hour_key(now)
    acc = state["acc"]
    if acc is None:
        state["acc"] = _new_acc(key, now)
        return False
    last = datetime.fromisoformat(acc["last"])
    seconds = (now - last).total_seconds()
    valid = None not in (pv_w, home_w, hp_w, wb_w) and 0 < seconds <= MAX_GAP_S

    def add(target: dict[str, Any], span: float) -> None:
        if not valid or span <= 0:
            return
        target["s"] += span
        target["pv"] += max(0.0, pv_w) * span
        target["home"] += max(0.0, home_w) * span
        target["hp"] += max(0.0, hp_w) * span
        target["wb"] += max(0.0, wb_w) * span

    if key == acc["h"]:
        add(acc, seconds)
        acc["last"] = now.astimezone(timezone.utc).isoformat()
        return False
    boundary = datetime.fromisoformat(acc["h"]) + timedelta(hours=1)
    add(acc, (boundary - last).total_seconds())
    _finalize(state, acc, now)
    fresh = _new_acc(key, now)
    if hour_start(now) == boundary:
        add(fresh, (now - boundary).total_seconds())
    state["acc"] = fresh
    return True


def prune(state: dict[str, Any], now: datetime) -> None:
    """Alles, was älter als 8 Tage ist, löschen."""
    limit = (hour_start(now) - timedelta(days=KEEP_DAYS)).isoformat()
    for name in ("fc", "ist"):
        for key in [k for k in state[name] if k < limit]:
            del state[name][key]
    day_limit = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    for key in [k for k in state["kosten_fc"] if k < day_limit]:
        del state["kosten_fc"][key]


def _wape(pairs: list[tuple[float, float]]) -> float | None:
    actual = sum(a for _, a in pairs)
    if actual <= 0:
        return None
    return max(0.0, 100.0 - sum(abs(f - a) for f, a in pairs) / actual * 100.0)


def evaluate(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Genauigkeit über die letzten 7 Tage. Gesamtwert erst ab 72 bewerteten Stunden."""
    start = (hour_start(now) - timedelta(days=WINDOW_DAYS)).isoformat()
    hours = [k for k in state["ist"] if k >= start and k in state["fc"]]
    pv = [(state["fc"][k]["pv"], state["ist"][k]["pv"]) for k in hours]
    base = [(state["fc"][k]["base"], state["ist"][k]["base"]) for k in hours]
    acc_pv, acc_base = _wape(pv), _wape(base)
    weights = [(acc_pv, sum(a for _, a in pv)), (acc_base, sum(a for _, a in base))]
    used = [(v, w) for v, w in weights if v is not None]
    total = sum(v * w for v, w in used) / sum(w for _, w in used) if used else None
    ready = len(hours) >= MIN_HOURS
    return {
        "wert": round(total) if ready and total is not None else None,
        "pv_genauigkeit": None if acc_pv is None else round(acc_pv, 1),
        "verbrauch_genauigkeit": None if acc_base is None else round(acc_base, 1),
        "bewertete_stunden": len(hours),
        "zeitraum_tage": WINDOW_DAYS,
        "status": "ok" if ready else "lernt",
        "letzte_bewertung": state["letzte"],
    }
