"""Datenabruf aus Home Assistant für Prognose und Planer (nur lesend)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def async_fetch_prices(hass: HomeAssistant, start: datetime, days: int = 3) -> dict[datetime, float]:
    """Tibber-Preise im 15-Minuten-Raster in ct/kWh. Leeres Ergebnis bei Fehler."""
    try:
        response = await hass.services.async_call(
            "tibber_prices",
            "get_price",
            {"start_time": start.isoformat(), "end_time": (start + timedelta(days=days)).isoformat()},
            blocking=True,
            return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - fremde Integration, jeder Fehler heißt: keine Preise
        _LOGGER.warning("Preise nicht abrufbar: %s", err)
        return {}
    out: dict[datetime, float] = {}
    for item in (response or {}).get("price_info") or []:
        when = dt_util.parse_datetime(str(item.get("startsAt")))
        total = item.get("total")
        if when is None or total is None:
            continue
        try:
            out[when] = float(total) * 100.0
        except (TypeError, ValueError):
            continue
    return out


async def async_fetch_temperatures(hass: HomeAssistant, entity_id: str) -> dict[datetime, float]:
    """Stündliche Temperaturprognose. Schlüssel: Stundenbeginn."""
    try:
        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Wetterprognose nicht abrufbar: %s", err)
        return {}
    out: dict[datetime, float] = {}
    for item in ((response or {}).get(entity_id) or {}).get("forecast") or []:
        when = dt_util.parse_datetime(str(item.get("datetime")))
        temp = item.get("temperature")
        if when is None or temp is None:
            continue
        try:
            out[when.replace(minute=0, second=0, microsecond=0)] = float(temp)
        except (TypeError, ValueError):
            continue
    return out


async def async_fetch_daily_means(hass: HomeAssistant, entity_id: str) -> list[tuple[date, float]]:
    """Tagesmitteltemperaturen der Prognose: Mittel aus Höchst- und Tiefstwert."""
    try:
        response = await hass.services.async_call(
            "weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"}, blocking=True, return_response=True
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Tagesprognose nicht abrufbar: %s", err)
        return []
    out: list[tuple[date, float]] = []
    for item in ((response or {}).get(entity_id) or {}).get("forecast") or []:
        when = dt_util.parse_datetime(str(item.get("datetime")))
        high, low = item.get("temperature"), item.get("templow")
        if when is None or high is None or low is None:
            continue
        try:
            out.append((dt_util.as_local(when).date(), (float(high) + float(low)) / 2.0))
        except (TypeError, ValueError):
            continue
    return out


def pv_hours(hass: HomeAssistant, entities: list[str], today: date) -> dict[date, dict[str, float]]:
    """Stundenwerte der PV-Prognose für heute, morgen, übermorgen aus dem Attribut 'hours'."""
    out: dict[date, dict[str, float]] = {}
    for offset, entity_id in enumerate(entities):
        state = hass.states.get(entity_id)
        hours = state.attributes.get("hours") if state else None
        if isinstance(hours, dict):
            out[today + timedelta(days=offset)] = dict(hours)
    return out


def _start(row: dict[str, Any]) -> datetime | None:
    value = row.get("start")
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        if value > 1e11:  # Millisekunden
            value = value / 1000.0
        return dt_util.utc_from_timestamp(value)
    return None


async def async_fetch_hourly_means(hass: HomeAssistant, entity_ids: list[str], days: int) -> dict[str, dict[datetime, float]]:
    """Stundenmittel aus der Langzeitstatistik. Leeres Ergebnis, wenn der Recorder fehlt."""
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        start = dt_util.utcnow() - timedelta(days=days)
        raw = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, None, set(entity_ids), "hour", None, {"mean"}
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Statistik nicht abrufbar: %s", err)
        return {}
    out: dict[str, dict[datetime, float]] = {}
    for entity_id, rows in (raw or {}).items():
        series: dict[datetime, float] = {}
        for row in rows:
            when, mean = _start(row), row.get("mean")
            if when is not None and mean is not None:
                series[when] = float(mean)
        out[entity_id] = series
    return out


def base_rows(home: dict[datetime, float], hp_w: dict[datetime, float], wallbox: dict[datetime, float]) -> list[tuple[datetime, float]]:
    """Grundlast je Stunde in W: Hausverbrauch ohne Wärmepumpe und Wallbox, örtliche Zeit."""
    rows = []
    for when, watts in home.items():
        rest = watts - hp_w.get(when, 0.0) - wallbox.get(when, 0.0)
        rows.append((dt_util.as_local(when), max(0.0, rest)))
    return rows


def heat_days(outdoor: dict[datetime, float], hp_w: dict[datetime, float]) -> list[tuple[float, float]]:
    """Tagesmittel (Außentemperatur, Wärmepumpenleistung W), nur vollständige Tage."""
    temps: dict[date, list[float]] = defaultdict(list)
    power: dict[date, list[float]] = defaultdict(list)
    for when, value in outdoor.items():
        temps[dt_util.as_local(when).date()].append(value)
    for when, value in hp_w.items():
        power[dt_util.as_local(when).date()].append(value)
    out = []
    for day, values in temps.items():
        if len(values) >= 20 and len(power.get(day, [])) >= 20:
            out.append((sum(values) / len(values), sum(power[day]) / len(power[day])))
    return out
